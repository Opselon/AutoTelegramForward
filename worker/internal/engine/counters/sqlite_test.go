package counters

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"sort"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	_ "github.com/mattn/go-sqlite3" // registers the sqlite3 driver
)

// ruleCountersSchema is the Core's migration v27 DDL, replayed verbatim so
// the store is tested against the real shape rather than a hand-rolled
// approximation.
const ruleCountersSchema = `
CREATE TABLE IF NOT EXISTS rule_counters (
    rule_id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL DEFAULT 0,
    every_n INTEGER NOT NULL DEFAULT 1,
    accepted INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rule_counters_owner ON rule_counters(owner_user_id);
`

// newSQLiteTestStore opens a fresh temp SQLite carrying the Core's
// rule_counters schema and the store over it.
func newSQLiteTestStore(t *testing.T) Store {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "atf.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	if _, err := db.Exec(ruleCountersSchema); err != nil {
		db.Close()
		t.Fatalf("apply schema: %v", err)
	}
	store, err := NewSQLite(db)
	if err != nil {
		db.Close()
		t.Fatalf("new sqlite counter store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// TestAdvanceReturnsAOneBasedSequence pins the primary contract: six
// advances yield 1..6, and the persisted row carries the same number.
func TestAdvanceReturnsAOneBasedSequence(t *testing.T) {
	store := newSQLiteTestStore(t)
	ctx := context.Background()
	seen := []int{}
	for i := 0; i < 6; i++ {
		n, err := store.Advance(ctx, "r1", 5, 5094837833)
		if err != nil {
			t.Fatalf("advance %d: %v", i, err)
		}
		seen = append(seen, n)
	}
	want := []int{1, 2, 3, 4, 5, 6}
	for i := range want {
		if seen[i] != want[i] {
			t.Fatalf("sequence = %v, want %v", seen, want)
		}
	}
	row, err := store.Peek(ctx, "r1")
	if err != nil {
		t.Fatalf("peek: %v", err)
	}
	if row == nil {
		t.Fatal("peek returned no row after six advances")
	}
	if row.Accepted != 6 || row.EveryN != 5 || row.OwnerUserID != 5094837833 {
		t.Errorf("row = %+v, want accepted=6 every_n=5 owner=5094837833", row)
	}
}

// TestAdvanceIsRestartDurable: a fresh store object over the same file
// continues the cadence (Python's test_restarts_do_not_reset_the_count).
func TestAdvanceIsRestartDurable(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "restart.db")
	ctx := context.Background()

	open := func() Store {
		t.Helper()
		db, err := sql.Open("sqlite3", path)
		if err != nil {
			t.Fatalf("open: %v", err)
		}
		if _, err := db.Exec(ruleCountersSchema); err != nil {
			db.Close()
			t.Fatalf("schema: %v", err)
		}
		store, err := NewSQLite(db)
		if err != nil {
			db.Close()
			t.Fatalf("store: %v", err)
		}
		return store
	}
	first := open()
	for i := 0; i < 5; i++ {
		if _, err := first.Advance(ctx, "r1", 3, 1); err != nil {
			t.Fatalf("advance %d: %v", i, err)
		}
	}
	_ = first.Close()

	second := open()
	defer second.Close()
	n, err := second.Advance(ctx, "r1", 3, 1)
	if err != nil {
		t.Fatalf("advance after restart: %v", err)
	}
	if n != 6 {
		t.Fatalf("advance after restart = %d, want 6 (the cadence must continue)", n)
	}
}

// TestChangingTheIntervalStartsAFreshCycle: the CASE inside the UPSERT
// reads the previous row, so a changed every_n resets accepted to 1 —
// Python's reset-on-interval-change.
func TestChangingTheIntervalStartsAFreshCycle(t *testing.T) {
	store := newSQLiteTestStore(t)
	ctx := context.Background()
	for i := 0; i < 5; i++ {
		if _, err := store.Advance(ctx, "r1", 3, 1); err != nil {
			t.Fatalf("advance %d: %v", i, err)
		}
	}
	// The owner re-tunes the cadence; the first message afterwards is
	// message 1 of the new cycle.
	if n, err := store.Advance(ctx, "r1", 5, 1); err != nil || n != 1 {
		t.Fatalf("first advance after change = %d (%v), want 1", n, err)
	}
	if n, err := store.Advance(ctx, "r1", 5, 1); err != nil || n != 2 {
		t.Fatalf("second advance after change = %d (%v), want 2", n, err)
	}
}

// TestCountersAreScopedToOneTask: one row per task, independent sequences.
func TestCountersAreScopedToOneTask(t *testing.T) {
	store := newSQLiteTestStore(t)
	ctx := context.Background()
	steps := []struct {
		rule string
		want int
	}{
		{"r1", 1}, {"r1", 2}, {"r2", 1}, {"r3", 1}, {"r1", 3},
	}
	for _, step := range steps {
		n, err := store.Advance(ctx, step.rule, 2, 999)
		if err != nil {
			t.Fatalf("advance %s: %v", step.rule, err)
		}
		if n != step.want {
			t.Errorf("advance %s = %d, want %d", step.rule, n, step.want)
		}
	}
}

// TestConcurrentAdvanceYieldsDistinctSequences is the idempotency
// guarantee: 40 concurrent advances must produce 40 distinct sequence
// numbers, because two workers that both read "next in 5" both forward.
//
// A read-then-write here would hand the same number to two goroutines; the
// UPSERT under SQLite's single-writer lock cannot.
func TestConcurrentAdvanceYieldsDistinctSequences(t *testing.T) {
	store := newSQLiteTestStore(t)
	ctx := context.Background()
	// A row exists from a previous run, as in the Python test.
	if _, err := store.Advance(ctx, "r1", 5, 1); err != nil {
		t.Fatalf("seed advance: %v", err)
	}

	const workers = 16
	var wg sync.WaitGroup
	results := make(chan int, workers)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			n, err := store.Advance(ctx, "r1", 5, 1)
			if err != nil {
				t.Errorf("concurrent advance: %v", err)
				return
			}
			results <- n
		}()
	}
	wg.Wait()
	close(results)

	got := []int{}
	for n := range results {
		got = append(got, n)
	}
	sort.Ints(got)
	// 1 (the seed) + 16 concurrent = 2..17, all distinct.
	if len(got) != workers {
		t.Fatalf("got %d results, want %d", len(got), workers)
	}
	for i := 1; i < len(got); i++ {
		if got[i] == got[i-1] {
			t.Fatalf("duplicate sequence %d — two workers own the same slot", got[i])
		}
	}
	row, _ := store.Peek(ctx, "r1")
	if row == nil || row.Accepted != workers+1 {
		t.Errorf("accepted = %v, want %d", row, workers+1)
	}
}

// TestResetMakesTheNextMessageFirstOfTheCycle mirrors Python's test_reset_…:
// reset drops the row, reset of an absent rule reports false.
func TestResetMakesTheNextMessageFirstOfTheCycle(t *testing.T) {
	store := newSQLiteTestStore(t)
	ctx := context.Background()
	for i := 0; i < 7; i++ {
		if _, err := store.Advance(ctx, "r1", 5, 1); err != nil {
			t.Fatalf("advance %d: %v", i, err)
		}
	}
	if row, _ := store.Peek(ctx, "r1"); row == nil || row.Accepted != 7 {
		t.Fatalf("seed sequence: %+v, want accepted=7", row)
	}
	existed, err := store.Reset(ctx, "r1")
	if err != nil || !existed {
		t.Fatalf("reset existing = %v (%v), want true", existed, err)
	}
	if existed, err := store.Reset(ctx, "nope"); err != nil || existed {
		t.Fatalf("reset absent = %v (%v), want false", existed, err)
	}
	n, err := store.Advance(ctx, "r1", 5, 1)
	if err != nil || n != 1 {
		t.Fatalf("advance after reset = %d (%v), want 1", n, err)
	}
	row, _ := store.Peek(ctx, "r1")
	if row == nil || row.Accepted != 1 {
		t.Errorf("accepted after reset = %v, want 1", row)
	}
}

// TestPeekOfAnUnknownRuleReturnsNil: no row, no error — the caller decides
// "no cadence yet" from a nil row, never from an error.
func TestPeekOfAnUnknownRuleReturnsNil(t *testing.T) {
	store := newSQLiteTestStore(t)
	row, err := store.Peek(context.Background(), "ghost")
	if err != nil {
		t.Fatalf("peek: %v", err)
	}
	if row != nil {
		t.Errorf("peek of unknown rule = %+v, want nil", row)
	}
}

// TestEmptyRuleIDIsRejected: the row's primary key cannot be empty, and a
// nonsense row must never be written.
func TestEmptyRuleIDIsRejected(t *testing.T) {
	store := newSQLiteTestStore(t)
	if _, err := store.Advance(context.Background(), "", 5, 1); err == nil {
		t.Error("Advance with empty rule_id returned nil error")
	}
}

// TestSchemaValidationFailsWithoutTheTable: the store refuses to start over
// a database the Core has not migrated, rather than looking healthy while
// every advance fails.
func TestSchemaValidationFailsWithoutTheTable(t *testing.T) {
	dir := t.TempDir()
	db, err := sql.Open("sqlite3", filepath.Join(dir, "empty.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if _, err := NewSQLite(db); err == nil {
		t.Error("NewSQLite over a schema-less db returned nil error")
	}
}

// TestAdvanceFoldsANonPositiveInterval mirrors Python's every_n guard:
// the row must never carry a 0 or negative stride.
func TestAdvanceFoldsANonPositiveInterval(t *testing.T) {
	store := newSQLiteTestStore(t)
	ctx := context.Background()
	for _, bad := range []int{0, -3} {
		if _, err := store.Advance(ctx, "r1", bad, 1); err != nil {
			t.Fatalf("advance with every_n=%d: %v", bad, err)
		}
	}
	row, err := store.Peek(ctx, "r1")
	if err != nil {
		t.Fatalf("peek: %v", err)
	}
	if row == nil || row.EveryN != 1 || row.Accepted != 2 {
		t.Errorf("row = %+v, want every_n=1 accepted=2", row)
	}
}

// TestAdvanceUpdatesTheRowTimestamp keeps the operator-visible column
// honest: updated_at moves forward on every accepted message.
func TestAdvanceUpdatesTheRowTimestamp(t *testing.T) {
	store := newSQLiteTestStore(t)
	ctx := context.Background()
	before := time.Now().Unix()
	if _, err := store.Advance(ctx, "r1", 4, 1); err != nil {
		t.Fatalf("advance: %v", err)
	}
	row, err := store.Peek(ctx, "r1")
	if err != nil || row == nil {
		t.Fatalf("peek: %v (%v)", row, err)
	}
	if row.UpdatedAt < before {
		t.Errorf("updated_at = %d, want >= %d", row.UpdatedAt, before)
	}
}

// TestSequencesInterleaveAcrossRules proves the store is not a global
// counter: two rules advance independently even when alternated.
func TestSequencesInterleaveAcrossRules(t *testing.T) {
	store := newSQLiteTestStore(t)
	ctx := context.Background()
	for i := 0; i < 4; i++ {
		for _, rule := range []string{"a", "b"} {
			n, err := store.Advance(ctx, rule, 3, 1)
			if err != nil {
				t.Fatalf("advance %s: %v", rule, err)
			}
			if n != i+1 {
				t.Errorf("advance %s round %d = %d, want %d", rule, i, n, i+1)
			}
		}
	}
}

var (
	_ = fmt.Sprintf
	_ = atomic.AddInt64
)
