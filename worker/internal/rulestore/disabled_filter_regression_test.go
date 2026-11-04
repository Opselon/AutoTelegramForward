package rulestore

import (
	"context"
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// zzDisabledFilterSchema is the v30+ filter_rules shape: the optional
// columns (regex_block, regex_require, is_enabled) exist as real columns.
const zzDisabledFilterSchema = `CREATE TABLE filter_rules (
	id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
	is_enabled INTEGER NOT NULL DEFAULT 1,
	whitelist_keywords TEXT NOT NULL DEFAULT '[]',
	blacklist_keywords TEXT NOT NULL DEFAULT '[]',
	regex_patterns TEXT NOT NULL DEFAULT '[]',
	allowed_media_types TEXT NOT NULL DEFAULT '[]',
	blocked_media_types TEXT NOT NULL DEFAULT '[]',
	drop_service_messages INTEGER NOT NULL DEFAULT 0,
	min_message_length INTEGER NOT NULL DEFAULT 0,
	max_message_length INTEGER NOT NULL DEFAULT 0,
	owner_user_id INTEGER NOT NULL DEFAULT 0,
	regex_block TEXT NOT NULL DEFAULT '[]',
	regex_require TEXT NOT NULL DEFAULT '[]'
)`

// TestGetFilterDisabledFilterStaysDisabled is the regression test for a real
// production defect: the projection cursor was held open (defer rows.Close())
// while the optional* helpers issued new queries through the SAME single
// connection (MaxOpenConns(1)). The helpers blocked until queryTimeout fired
// and returned an error, so is_enabled was never read and a filter the owner
// had toggled OFF silently stayed ENABLED — still deciding, still dropping
// messages. The fix closes the cursor before the optional column reads.
func TestGetFilterDisabledFilterStaysDisabled(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/t.db"
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(zzDisabledFilterSchema); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO filter_rules (id, is_enabled, blacklist_keywords) VALUES ('f1', 0, '[\"BAD\"]')"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	s, err := New(ctx, Config{SQLite: DSN{Raw: path, Label: "sqlite:t.db", Backend: "sqlite"}})
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	f, err := s.getFilter(ctx, "f1", 1)
	if err != nil {
		t.Fatalf("getFilter: %v", err)
	}
	if f == nil {
		t.Fatal("getFilter returned nil for an existing row")
	}
	if f.IsEnabled {
		t.Fatal("a filter with is_enabled=0 was read back as enabled — the optional-column read is failing, silently resurrecting a disabled filter")
	}
	if len(f.BlacklistKeywords) != 1 || f.BlacklistKeywords[0] != "BAD" {
		t.Fatalf("blacklist keywords lost: %v", f.BlacklistKeywords)
	}
}
