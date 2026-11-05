package ops

import (
	"context"
	"database/sql"
	"encoding/json"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3" // registers the sqlite3 driver the rules table opens with
)

// TestReadRulesForTasksFeatures pins the §6 feature flags the per-task
// console renders: the join must read filter/ai/replacements/header off the
// forward_rules row and forward_every_n out of the metadata blob, so the
// panel's F/R/AI/C/N badges show data instead of "unavailable".
//
// The test builds a real (file-backed) rules table with the columns the
// worker's own projections name — not a hand-rolled schema that could drift
// from production and silently stop matching.
func TestReadRulesForTasksFeatures(t *testing.T) {
	dsn := filepath.Join(t.TempDir(), "rules.db")
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		t.Fatalf("open rules db: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	schema := `
	CREATE TABLE forward_rules (
		id TEXT PRIMARY KEY,
		is_active INTEGER NOT NULL DEFAULT 1,
		source_chat_id TEXT NOT NULL DEFAULT '',
		target_chat_id TEXT NOT NULL DEFAULT '',
		updated_at INTEGER NOT NULL DEFAULT 0,
		filter_rule_id TEXT,
		ai_config_id TEXT,
		replacements TEXT NOT NULL DEFAULT '{}',
		header TEXT NOT NULL DEFAULT '',
		custom_header TEXT NOT NULL DEFAULT '',
		header_enabled INTEGER NOT NULL DEFAULT 0,
		metadata TEXT NOT NULL DEFAULT '{}'
	)`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("apply schema: %v", err)
	}

	insert := func(id string, active int, filterID, aiID, repl, customHdr string, hdrEnabled int, meta string) {
		metaJSON := meta
		if metaJSON == "" {
			metaJSON = "{}"
		}
		_, err := db.ExecContext(context.Background(), `
			INSERT INTO forward_rules (id, is_active, source_chat_id, target_chat_id,
			                            filter_rule_id, ai_config_id, replacements, header,
			                            custom_header, header_enabled, metadata)
			VALUES (?, ?, '-1001', '-1002', ?, ?, ?, 'h', ?, ?, ?)`,
			id, active, filterID, aiID, repl, customHdr, hdrEnabled, metaJSON)
		if err != nil {
			t.Fatalf("insert %s: %v", id, err)
		}
	}
	metaWithFEN := func(n int) string {
		b, _ := json.Marshal(map[string]any{"forward_every_n": n})
		return string(b)
	}

	// Every feature ON (filter + replacement + ai + cleaner + FEN 4).
	insert("task-all", 1, "flt-1", "ai-1", `{"a":"b"}`, "cleaned caption", 1, metaWithFEN(4))
	// Nothing ON: empty filter, empty ai, empty replacements, header off.
	insert("task-none", 1, "", "", "{}", "", 0, "{}")

	got, err := ReadRulesForTasks(context.Background(), db, "sqlite", []string{"task-all", "task-none"})
	if err != nil {
		t.Fatalf("ReadRulesForTasks: %v", err)
	}

	all := got["task-all"]
	if all == nil || !all.hasRow {
		t.Fatal("task-all missing from the join")
	}
	if !all.filterOn {
		t.Error("filter flag false with filter_rule_id set — panel would hide the F badge")
	}
	if !all.replacement {
		t.Error("replacement flag false with a non-empty replacements map — panel would hide the R badge")
	}
	if !all.aiOn {
		t.Error("ai flag false with ai_config_id set — panel would hide the AI badge")
	}
	if !all.cleanerOn {
		t.Error("cleaner flag false with header_enabled + a custom header — panel would hide the C badge")
	}
	if all.forwardN != 4 {
		t.Errorf("forward_every_n = %d, want 4 read out of the metadata blob", all.forwardN)
	}

	none := got["task-none"]
	if none == nil || !none.hasRow {
		t.Fatal("task-none missing from the join")
	}
	if none.filterOn || none.replacement || none.aiOn || none.cleanerOn || none.forwardN != 0 {
		t.Errorf("task-none flags should all be off: %+v", none)
	}

	// An unknown id yields no row at all — the caller renders "rule gone",
	// never a fabricated feature set.
	if _, ok := got["does-not-exist"]; ok {
		t.Error("unknown rule id returned a row; the panel would render a rule that does not exist")
	}
}
