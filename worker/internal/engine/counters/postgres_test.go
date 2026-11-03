package counters

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

// PostgreSQL integration suite for the forward-every-N cadence store.
//
// These tests run against a REAL PostgreSQL database, gated on
// ATF_TEST_POSTGRES_DSN: the whole point is to prove that the ON CONFLICT
// DO UPDATE row-level lock serializes concurrent advancers — the property
// that makes two workers unable to both own slot N. SQLite's single-writer
// lock cannot prove it.
//
// When ATF_TEST_POSTGRES_DSN is unset the suite is skipped. CI runs it
// against the real postgres:17-alpine service, mirroring the queue and
// scheduler integration suites.
//
// Each test creates a fresh scratch database, applies the production
// rule_counters DDL (the same statements initialize_postgres_schema
// executes), and drops it on cleanup — no shared state, no leftover rows.

// pgRuleCountersFixture is the PostgreSQL shape of migration v27.
const pgRuleCountersFixture = `
CREATE TABLE IF NOT EXISTS rule_counters (
    rule_id TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL DEFAULT 0,
    every_n INTEGER NOT NULL DEFAULT 1,
    accepted INTEGER NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rule_counters_owner ON rule_counters(owner_user_id);
`

var pgTestDBSeq uint64

func pgTestStore(t *testing.T) (Store, *sql.DB) {
	t.Helper()
	rawDSN := strings.TrimSpace(os.Getenv("ATF_TEST_POSTGRES_DSN"))
	if rawDSN == "" {
		t.Skip("ATF_TEST_POSTGRES_DSN is unset — skipping PostgreSQL integration tests")
		return nil, nil
	}
	rootConn, err := sql.Open("pgx", rawDSN)
	if err != nil {
		t.Fatalf("open root pg: %v", err)
	}
	defer rootConn.Close()

	seq := atomic.AddUint64(&pgTestDBSeq, 1)
	dbName := fmt.Sprintf("atf_test_counters_%d_%d", time.Now().UnixNano(), seq)
	if _, err := rootConn.Exec(fmt.Sprintf(`CREATE DATABASE "%s"`, dbName)); err != nil {
		t.Fatalf("create scratch db %s: %v", dbName, err)
	}

	u, err := url.Parse(rawDSN)
	if err != nil {
		t.Fatalf("parse dsn: %v", err)
	}
	u.Path = "/" + dbName
	testDSN := u.String()

	testConn, err := sql.Open("pgx", testDSN)
	if err != nil {
		t.Fatalf("open scratch db: %v", err)
	}
	if _, err := testConn.Exec(pgRuleCountersFixture); err != nil {
		testConn.Close()
		t.Fatalf("apply pg schema: %v", err)
	}

	t.Cleanup(func() {
		testConn.Close()
		cleanConn, err := sql.Open("pgx", rawDSN)
		if err == nil {
			_, _ = cleanConn.Exec(fmt.Sprintf(`DROP DATABASE IF EXISTS "%s" WITH (FORCE)`, dbName))
			cleanConn.Close()
		}
	})

	store, err := NewPostgres(testConn)
	if err != nil {
		t.Fatalf("new postgres counter store: %v", err)
	}
	return store, testConn
}

// TestPostgresAdvanceSequenceIsOneBased pins the primary contract on
// PostgreSQL: six advances yield 1..6 and the row carries the same number.
func TestPostgresAdvanceSequenceIsOneBased(t *testing.T) {
	store, _ := pgTestStore(t)
	ctx := context.Background()
	for i := 0; i < 6; i++ {
		n, err := store.Advance(ctx, "r1", 5, 5094837833)
		if err != nil {
			t.Fatalf("advance %d: %v", i, err)
		}
		if n != i+1 {
			t.Errorf("advance %d = %d, want %d", i, n, i+1)
		}
	}
	row, err := store.Peek(ctx, "r1")
	if err != nil {
		t.Fatalf("peek: %v", err)
	}
	if row == nil || row.Accepted != 6 || row.EveryN != 5 || row.OwnerUserID != 5094837833 {
		t.Errorf("row = %+v, want accepted=6 every_n=5 owner=5094837833", row)
	}
}

// TestPostgresChangingTheIntervalStartsAFreshCycle: the CASE inside the
// UPSERT reads the pre-conflict row, so a changed every_n resets the count.
func TestPostgresChangingTheIntervalStartsAFreshCycle(t *testing.T) {
	store, _ := pgTestStore(t)
	ctx := context.Background()
	for i := 0; i < 5; i++ {
		if _, err := store.Advance(ctx, "r1", 3, 1); err != nil {
			t.Fatalf("advance %d: %v", i, err)
		}
	}
	if n, err := store.Advance(ctx, "r1", 5, 1); err != nil || n != 1 {
		t.Fatalf("first advance after change = %d (%v), want 1", n, err)
	}
	if n, err := store.Advance(ctx, "r1", 5, 1); err != nil || n != 2 {
		t.Fatalf("second advance after change = %d (%v), want 2", n, err)
	}
}

// TestPostgresConcurrentAdvanceYieldsDistinctSequences is THE test the
// PostgreSQL backend exists for: 40 concurrent advancers over one rule
// must produce 40 distinct sequence numbers.
//
// The ON CONFLICT DO UPDATE takes a row-level lock on the conflicting
// tuple, so the second advancer's UPSERT blocks until the first commits;
// each read-back therefore observes its own increment. A read-then-write
// would hand the same number to two workers, and both would forward.
func TestPostgresConcurrentAdvanceYieldsDistinctSequences(t *testing.T) {
	store, _ := pgTestStore(t)
	ctx := context.Background()
	if _, err := store.Advance(ctx, "r1", 5, 1); err != nil {
		t.Fatalf("seed advance: %v", err)
	}

	const workers = 40
	var wg sync.WaitGroup
	results := make(chan int, workers)
	errs := make(chan error, workers)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			n, err := store.Advance(ctx, "r1", 5, 1)
			results <- n
			errs <- err
		}()
	}
	wg.Wait()
	close(results)
	close(errs)

	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent advance: %v", err)
		}
	}
	got := []int{}
	for n := range results {
		got = append(got, n)
	}
	if len(got) != workers {
		t.Fatalf("got %d results, want %d", len(got), workers)
	}
	sort.Ints(got)
	// The seed left accepted=1, so the 40 concurrent advancers must own
	// 2..41 — all distinct, in any order.
	want := make([]int, 0, workers)
	for n := 2; n <= workers+1; n++ {
		want = append(want, n)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("sequences = %v, want %v (a duplicate means two workers own one slot)", got, want)
		}
	}
	row, _ := store.Peek(ctx, "r1")
	if row == nil || row.Accepted != workers+1 {
		t.Errorf("accepted = %v, want %d", row, workers+1)
	}
}

// TestPostgresResetStartsAFreshCycle verifies reset on PostgreSQL.
func TestPostgresResetStartsAFreshCycle(t *testing.T) {
	store, _ := pgTestStore(t)
	ctx := context.Background()
	for i := 0; i < 7; i++ {
		if _, err := store.Advance(ctx, "r1", 5, 1); err != nil {
			t.Fatalf("advance %d: %v", i, err)
		}
	}
	existed, err := store.Reset(ctx, "r1")
	if err != nil || !existed {
		t.Fatalf("reset existing = %v (%v), want true", existed, err)
	}
	if existed, err := store.Reset(ctx, "nope"); err != nil || existed {
		t.Fatalf("reset absent = %v (%v), want false", existed, err)
	}
	if n, err := store.Advance(ctx, "r1", 5, 1); err != nil || n != 1 {
		t.Fatalf("advance after reset = %d (%v), want 1", n, err)
	}
}

// TestPostgresSchemaValidationFailsWithoutTheTable: the store refuses to
// start over a database the Core has not bootstrapped.
func TestPostgresSchemaValidationFailsWithoutTheTable(t *testing.T) {
	_, db := pgTestStore(t)
	if db == nil {
		return
	}
	// Scratch database of the harness already carries rule_counters; drop
	// it to prove the constructor fails closed.
	if _, err := db.Exec(`DROP TABLE rule_counters`); err != nil {
		t.Fatalf("drop rule_counters: %v", err)
	}
	if _, err := NewPostgres(db); err == nil {
		t.Error("NewPostgres without rule_counters returned nil error")
	}
}

// TestPostgresAdvanceFoldsANonPositiveInterval mirrors Python's every_n
// guard on PostgreSQL: a 0 or negative stride never reaches the row.
func TestPostgresAdvanceFoldsANonPositiveInterval(t *testing.T) {
	store, _ := pgTestStore(t)
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
