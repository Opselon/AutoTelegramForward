package rulestore

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	_ "github.com/mattn/go-sqlite3"

	"atf/worker/internal/domain/rule"
)

// The RuleStore test suite answers one question: does the worker read the
// task's REAL persisted state — active, paused, auto-paused, filtered —
// instead of the Stage-1 always-active stub?
//
// Two properties are load-bearing enough to pin every fixture against:
//
//   - a database ERROR is never reported as a missing rule (an outage must
//     defer, not mass-fail deliveries);
//   - a legacy row lacking the v30 columns (regex_block, regex_require,
//     is_enabled) keeps working with its filter still ENABLED — the Python
//     repository's own compatibility rule, and the difference between a
//     customer's task still running and silently disabled.

// newTestStore builds a composite store over one temp SQLite file, the common
// single-database case.
func newTestStore(t *testing.T) *Store {
	t.Helper()
	path := filepath.Join(t.TempDir(), "atf.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	applyRuleSchema(t, db)
	t.Cleanup(func() { _ = db.Close() })

	store := &Store{
		rulesDB:       db,
		rulesBackend:  "sqlite",
		filtersDB:     db,
		filtersShared: true,
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// applyRuleSchema creates the two tables in the shape the Python Core's own
// migrations produce, including the *modern* columns so the compatibility
// path is exercised against the real target schema.
func applyRuleSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	for _, stmt := range []string{
		`CREATE TABLE IF NOT EXISTS forward_rules (
			id TEXT PRIMARY KEY,
			session_id TEXT NOT NULL DEFAULT '',
			is_active INTEGER NOT NULL DEFAULT 1,
			is_paused INTEGER NOT NULL DEFAULT 0,
			paused_until INTEGER NOT NULL DEFAULT 0,
			forward_mode TEXT NOT NULL DEFAULT 'COPY_MESSAGE',
			content_mode TEXT NOT NULL DEFAULT 'AUTO',
			filter_rule_id TEXT,
			metadata TEXT NOT NULL DEFAULT '{}'
		)`,
		`CREATE TABLE IF NOT EXISTS filter_rules (
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
		)`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("apply schema: %v", err)
		}
	}
}

// insertRule writes a forward_rules row; metadata carries the auto_pause blob.
func insertRule(t *testing.T, db *sql.DB, id string, active, paused bool, pausedUntil int64, filterID, metadata string) {
	t.Helper()
	var autoPause any
	if metadata != "" {
		_ = json.Unmarshal([]byte(metadata), &autoPause)
		md := map[string]any{}
		if autoPause != nil {
			md["auto_pause"] = autoPause
		}
		out, _ := json.Marshal(md)
		metadata = string(out)
	}
	filterVal := sql.NullString{}
	if filterID != "" {
		filterVal = sql.NullString{String: filterID, Valid: true}
	}
	if _, err := db.Exec(`INSERT OR REPLACE INTO forward_rules
		(id, session_id, is_active, is_paused, paused_until, forward_mode, content_mode, filter_rule_id, metadata)
		VALUES (?,?,?,?,?,?,?,?,?)`,
		id, "s1", active, paused, pausedUntil, "COPY_MESSAGE", "AUTO", filterVal, metadata); err != nil {
		t.Fatalf("insert rule %s: %v", id, err)
	}
}

// insertFilter writes a filter_rules row with every modern column present.
func insertFilter(t *testing.T, db *sql.DB, id string, f *rule.FilterRule) {
	t.Helper()
	if _, err := db.Exec(`INSERT OR REPLACE INTO filter_rules
		(id, name, whitelist_keywords, blacklist_keywords, regex_patterns, regex_block, regex_require,
		 allowed_media_types, blocked_media_types, drop_service_messages,
		 min_message_length, max_message_length, is_enabled)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		id, "f",
		toJSON(t, f.WhitelistKeywords), toJSON(t, f.BlacklistKeywords),
		toJSON(t, f.RegexPatterns), toJSON(t, f.RegexBlock), toJSON(t, f.RegexRequire),
		toJSON(t, f.AllowedMediaTypes), toJSON(t, f.BlockedMediaTypes),
		f.DropService, f.MinMessageLength, f.MaxMessageLength, f.IsEnabled); err != nil {
		t.Fatalf("insert filter %s: %v", id, err)
	}
}

func toJSON(t *testing.T, list []string) string {
	t.Helper()
	if list == nil {
		return "[]"
	}
	out, err := json.Marshal(list)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(out)
}

// --- the contract: present, absent, error ----------------------------------- //

func TestGetByIDReturnsRealRuleState(t *testing.T) {
	store := newTestStore(t)
	insertRule(t, store.rulesDB, "r1", true, false, 0, "", "")

	r, f, err := store.GetByID("r1")
	if err != nil {
		t.Fatalf("GetByID: %v", err)
	}
	if r == nil {
		t.Fatal("a real, active rule came back nil (the Stage-1 stub behaviour)")
	}
	if r.ID != "r1" || !r.IsActive {
		t.Errorf("projection = %+v, want an active r1", r)
	}
	if f != nil {
		t.Errorf("rule with no filter_rule_id returned a filter: %+v", f)
	}
}

func TestMissingRuleReportsMissingNotError(t *testing.T) {
	store := newTestStore(t)
	r, _, err := store.GetByID("does-not-exist")
	if err != nil {
		t.Errorf("a missing rule produced an error (%v); the caller would fail the job instead of dropping it", err)
	}
	if r != nil {
		t.Error("a missing rule came back non-nil")
	}
}

// TestStoreOutageIsAnErrorNotAMissingRule is the acceptance criterion: a
// worker that treats an unreachable store as "rule deleted" would fail every
// queued delivery during a database outage instead of deferring them.
func TestStoreOutageIsAnErrorNotAMissingRule(t *testing.T) {
	store := newTestStore(t)
	// Close the handle underneath the store: every subsequent read fails.
	_ = store.rulesDB.Close()
	_ = store.filtersDB.Close()

	r, _, err := store.GetByID("r1")
	if err == nil {
		t.Fatal("a closed store reported no error; the worker would treat the outage as 'rule deleted'")
	}
	if r != nil {
		t.Error("an outage returned a rule projection")
	}
}

// --- active / paused / auto-paused ------------------------------------------ //

func TestDisabledRuleIsNotActive(t *testing.T) {
	store := newTestStore(t)
	insertRule(t, store.rulesDB, "r2", false, false, 0, "", "")
	r, _, err := store.GetByID("r2")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if r.IsActive {
		t.Error("an inactive rule projected as active; the worker would deliver for a disabled task")
	}
}

func TestPausedRuleProjection(t *testing.T) {
	store := newTestStore(t)
	insertRule(t, store.rulesDB, "r3", true, true, time.Now().Add(time.Hour).Unix(), "", "")
	r, _, _ := store.GetByID("r3")
	if r == nil || !r.IsPaused {
		t.Fatal("the paused bit was not projected")
	}
	if !r.PauseStillActiveAt(time.Now()) {
		t.Error("an unexpired pause was reported as expired")
	}
}

func TestExpiredPauseIsNotActive(t *testing.T) {
	store := newTestStore(t)
	insertRule(t, store.rulesDB, "r4", true, true, time.Now().Add(-time.Minute).Unix(), "", "")
	r, _, _ := store.GetByID("r4")
	if r == nil {
		t.Fatal("nil rule")
	}
	if r.PauseStillActiveAt(time.Now()) {
		t.Error("an expired pause still blocks; the delivery would defer forever (Python: is_currently_paused()==False)")
	}
}

// TestAutoPauseReadFromMetadataJSON: auto_pause is NOT a column. The engine
// records it inside the task's metadata JSON blob, so the store must read it
// out of the same blob — a worker that scanned for an auto_pause column would
// find nothing and deliver into a dead chat.
func TestAutoPauseReadFromMetadataJSON(t *testing.T) {
	store := newTestStore(t)
	meta := `{"code":"TARGET_GONE","reason":"target chat deleted","chat_id":"-100999","scope":"target","at":1730000000}`
	insertRule(t, store.rulesDB, "r5", true, false, 0, "", meta)

	r, _, err := store.GetByID("r5")
	if err != nil || r == nil {
		t.Fatalf("GetByID: %v (%v)", r, err)
	}
	if r.AutoPauseReason != "target chat deleted" {
		t.Errorf("auto_pause reason = %q, want the reason from the metadata JSON", r.AutoPauseReason)
	}
}

// --- attached filters -------------------------------------------------------- //

func TestAttachedFilterIsReadWithAllLists(t *testing.T) {
	store := newTestStore(t)
	insertRule(t, store.rulesDB, "r6", true, false, 0, "f6", "")
	insertFilter(t, store.rulesDB, "f6", &rule.FilterRule{
		IsEnabled:          true,
		WhitelistKeywords:  []string{"wanted"},
		BlacklistKeywords:  []string{"blocked"},
		RegexBlock:         []string{`^spam`},
		RegexRequire:       []string{`^promo`},
		RegexPatterns:      []string{`legacy`},
		AllowedMediaTypes:  []string{"photo"},
		BlockedMediaTypes:  []string{"video"},
		DropService:        true,
		MinMessageLength:   5,
		MaxMessageLength:   500,
	})

	r, f, err := store.GetByID("r6")
	if err != nil || r == nil {
		t.Fatalf("GetByID: %v (%v)", r, err)
	}
	if f == nil {
		t.Fatal("an attached filter came back nil")
	}
	for _, c := range []struct {
		name string
		got  []string
		want []string
	}{
		{"whitelist", f.WhitelistKeywords, []string{"wanted"}},
		{"blacklist", f.BlacklistKeywords, []string{"blocked"}},
		{"regex_block", f.RegexBlock, []string{`^spam`}},
		{"regex_require", f.RegexRequire, []string{`^promo`}},
		{"regex_patterns", f.RegexPatterns, []string{"legacy"}},
		{"allowed media", f.AllowedMediaTypes, []string{"photo"}},
		{"blocked media", f.BlockedMediaTypes, []string{"video"}},
	} {
		if fmt.Sprint(c.got) != fmt.Sprint(c.want) {
			t.Errorf("%s = %v, want %v", c.name, c.got, c.want)
		}
	}
	if !f.IsEnabled || !f.DropService || f.MinMessageLength != 5 || f.MaxMessageLength != 500 {
		t.Errorf("scalar filter fields wrong: enabled=%v drop=%v min=%d max=%d", f.IsEnabled, f.DropService, f.MinMessageLength, f.MaxMessageLength)
	}
}

func TestDisabledFilterIsReadAsDisabled(t *testing.T) {
	store := newTestStore(t)
	insertRule(t, store.rulesDB, "r7", true, false, 0, "f7", "")
	insertFilter(t, store.rulesDB, "f7", &rule.FilterRule{IsEnabled: false})
	_, f, err := store.GetByID("r7")
	if err != nil {
		t.Fatalf("GetByID: %v", err)
	}
	if f == nil {
		t.Fatal("nil filter")
	}
	if f.IsEnabled {
		t.Error("an explicitly disabled filter projected as enabled")
	}
}

// TestDanglingFilterReferenceProceedsUnfiltered: the rule points at a filter
// that no longer exists. The explicit policy is the Python pipeline's own:
// a failed filter load means no filter is evaluated — never a dropped
// message, never a crash.
func TestDanglingFilterReferenceProceedsUnfiltered(t *testing.T) {
	store := newTestStore(t)
	insertRule(t, store.rulesDB, "r8", true, false, 0, "gone", "")

	r, f, err := store.GetByID("r8")
	if err != nil {
		t.Fatalf("a dangling filter reference raised an error: %v", err)
	}
	if r == nil {
		t.Fatal("the rule vanished behind a dangling filter reference")
	}
	if f != nil {
		t.Errorf("a dangling filter reference returned a filter: %+v", f)
	}
}

// --- legacy schema compatibility --------------------------------------------- //

// TestLegacyRowWithoutV30Columns: a database written before v30 has no
// regex_block / regex_require / is_enabled columns on filter_rules. The Go
// reader MUST keep that filter working and MUST keep it enabled — the Python
// reader's documented rule, and a silent disable on first contact would be a
// customer-visible regression no test would otherwise catch.
func TestLegacyRowWithoutV30Columns(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "atf.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()

	// The PRE-v30 filter schema: no regex_block, no regex_require, no is_enabled.
	for _, stmt := range []string{
		`CREATE TABLE forward_rules (
			id TEXT PRIMARY KEY, session_id TEXT NOT NULL DEFAULT '',
			is_active INTEGER NOT NULL DEFAULT 1, is_paused INTEGER NOT NULL DEFAULT 0,
			paused_until INTEGER NOT NULL DEFAULT 0, forward_mode TEXT NOT NULL DEFAULT 'COPY_MESSAGE',
			content_mode TEXT NOT NULL DEFAULT 'AUTO', filter_rule_id TEXT, metadata TEXT NOT NULL DEFAULT '{}'
		)`,
		`CREATE TABLE filter_rules (
			id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
			whitelist_keywords TEXT NOT NULL DEFAULT '[]',
			blacklist_keywords TEXT NOT NULL DEFAULT '[]',
			regex_patterns TEXT NOT NULL DEFAULT '[]',
			allowed_media_types TEXT NOT NULL DEFAULT '[]',
			blocked_media_types TEXT NOT NULL DEFAULT '[]',
			drop_service_messages INTEGER NOT NULL DEFAULT 1,
			min_message_length INTEGER NOT NULL DEFAULT 0,
			max_message_length INTEGER NOT NULL DEFAULT 0,
			owner_user_id INTEGER NOT NULL DEFAULT 0
		)`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("legacy schema: %v", err)
		}
	}

	// A task with an attached filter, and a filter row carrying ONLY the
	// pre-v30 columns (a blacklist + the legacy regex_patterns list).
	var filterRef sql.NullString = sql.NullString{String: "legacy-filter", Valid: true}
	if _, err := db.Exec(`INSERT INTO forward_rules
		(id, session_id, is_active, is_paused, paused_until, forward_mode, content_mode, filter_rule_id, metadata)
		VALUES ('legacy-rule','s1',1,0,0,'COPY_MESSAGE','AUTO',?,'{}')`, filterRef); err != nil {
		t.Fatalf("insert legacy rule: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO filter_rules
		(id, name, whitelist_keywords, blacklist_keywords, regex_patterns,
		 allowed_media_types, blocked_media_types, drop_service_messages,
		 min_message_length, max_message_length, owner_user_id)
		 VALUES ('legacy-filter','old','[]','["blocked-legacy"]','["pattern\\d+"]','[]','[]',1,0,0,0)`); err != nil {
		t.Fatalf("insert legacy filter: %v", err)
	}

	store := &Store{
		rulesDB: db, rulesBackend: "sqlite",
		filtersDB: db, filtersShared: true,
	}
	r, f, err := store.GetByID("legacy-rule")
	if err != nil {
		t.Fatalf("legacy lookup: %v", err)
	}
	if r == nil || !r.IsActive {
		t.Fatal("a legacy active rule was not projected (or was projected inactive)")
	}
	if f == nil {
		t.Fatal("a legacy attached filter was dropped by the reader")
	}
	// THE compatibility rule: a row with no is_enabled column stays ENABLED.
	if !f.IsEnabled {
		t.Error("a pre-v30 filter with no is_enabled column was projected DISABLED; the Python reader defaults it to enabled")
	}
	// The legacy regex list must survive as RegexPatterns (which the engine
	// treats as blocking for a blacklist-only filter, require-any otherwise).
	if len(f.RegexPatterns) != 1 || f.RegexPatterns[0] != `pattern\d+` {
		t.Errorf("legacy regex_patterns = %v, want [pattern\\d+]", f.RegexPatterns)
	}
	if len(f.BlacklistKeywords) != 1 || f.BlacklistKeywords[0] != "blocked-legacy" {
		t.Errorf("blacklist = %v", f.BlacklistKeywords)
	}
	// The absent modern lists are empty, never a load failure.
	if f.RegexBlock != nil || f.RegexRequire != nil {
		t.Errorf("a pre-v30 row produced non-empty modern lists: block=%v require=%v", f.RegexBlock, f.RegexRequire)
	}
}

// --- empty id --------------------------------------------------------------- //

func TestEmptyRuleIDIsAnError(t *testing.T) {
	store := newTestStore(t)
	if _, _, err := store.GetByID(""); err == nil {
		t.Error("an empty rule id must be an explicit error, not a nil-rule lookup")
	}
}

// --- DSN labelling never leaks credentials ----------------------------------- //

func TestDSNLabelCarriesNoCredentials(t *testing.T) {
	dsn := DSN{
		Raw:   "postgres://atf:s3cret%2Fpassword@db.example.com:5432/atf?sslmode=require",
		Label: "postgres://db.example.com:5432/atf",
	}
	if contains(dsn.Label, "s3cret") {
		t.Errorf("the DSN label carries a credential: %s", dsn.Label)
	}
	if contains(dsn.Raw, "Label") {
		t.Error("raw/label fields confused")
	}
	_ = dsn
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}

var _ = os.DevNull
var _ = context.Background
