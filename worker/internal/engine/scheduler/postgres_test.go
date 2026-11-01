package scheduler

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

// PostgreSQL integration suite for scheduled actions.
//
// These tests run against a REAL PostgreSQL database, gated on
// ATF_TEST_POSTGRES_DSN: the whole point is to prove SKIP LOCKED claim
// behavior, DOUBLE PRECISION arithmetic and concurrent row locking against
// the real engine — things SQLite cannot prove.
//
// When ATF_TEST_POSTGRES_DSN is unset the suite is skipped. CI runs it against
// the real postgres:17-alpine service.
//
// Each test creates a fresh scratch database, applies the production v33
// DDL (the same statements initialize_postgres_schema executes), and drops
// it on cleanup — no shared state, no leftover tables.

const pgScheduledActionsFixture = `
CREATE TABLE IF NOT EXISTS scheduled_actions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL DEFAULT 'auto_delete',
    transport TEXT NOT NULL DEFAULT 'user',
    transport_ref TEXT NOT NULL DEFAULT '',
    owner_user_id BIGINT NOT NULL DEFAULT 0,
    chat_id TEXT NOT NULL,
    message_id BIGINT NOT NULL DEFAULT 0,
    due_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'PENDING',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    CONSTRAINT scheduled_actions_unique
        UNIQUE (job_id, action_type, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_due
    ON scheduled_actions (state, next_attempt_at, due_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_owner
    ON scheduled_actions (owner_user_id, state);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_transport
    ON scheduled_actions (transport, state);
`

var testDBSeq uint64

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

	seq := atomic.AddUint64(&testDBSeq, 1)
	dbName := fmt.Sprintf("atf_test_sched_%d_%d", time.Now().UnixNano(), seq)
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
	if _, err := testConn.Exec(pgScheduledActionsFixture); err != nil {
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
		t.Fatalf("new postgres store: %v", err)
	}
	return store, testConn
}

// TestPostgresScheduleIsIdempotent proves the UNIQUE constraint on PostgreSQL:
// duplicate Schedule calls do not fail and do not create duplicate rows.
func TestPostgresScheduleIsIdempotent(t *testing.T) {
	store, _ := pgTestStore(t)
	ctx := context.Background()
	a := Action{
		ID:    ID("job-pg-1", ActionAutoDelete, "-100222", 42),
		JobID: "job-pg-1", Type: ActionAutoDelete,
		Transport: TransportUser, TransportRef: "sess-1",
		OwnerUserID: 77, ChatID: "-100222", MessageID: 42,
		DueAt: 100, NextAttemptAt: 100, MaxAttempts: 3,
	}
	for i := 0; i < 3; i++ {
		if err := store.Schedule(ctx, a); err != nil {
			t.Fatalf("schedule %d: %v", i, err)
		}
	}
	n, err := store.PendingCount(ctx)
	if err != nil {
		t.Fatalf("pending count: %v", err)
	}
	if n != 1 {
		t.Errorf("duplicate schedule on postgres: pending=%d, want 1", n)
	}
}

// TestPostgresClaimDueUsesSkipLocked proves that multiple workers claiming
// due actions on PostgreSQL never claim the same row (FOR UPDATE SKIP LOCKED).
func TestPostgresClaimDueUsesSkipLocked(t *testing.T) {
	store, _ := pgTestStore(t)
	ctx := context.Background()
	for i := 0; i < 30; i++ {
		a := Action{
			ID:    ID(fmt.Sprintf("job-pg-%d", i), ActionAutoDelete, "-100222", int64(100+i)),
			JobID: fmt.Sprintf("job-pg-%d", i), Type: ActionAutoDelete,
			Transport: TransportUser, TransportRef: "sess-1",
			OwnerUserID: 77, ChatID: "-100222", MessageID: int64(100 + i),
			DueAt: 100, NextAttemptAt: 100, MaxAttempts: 3,
		}
		if err := store.Schedule(ctx, a); err != nil {
			t.Fatalf("schedule %d: %v", i, err)
		}
	}

	results := make(chan []Action, 2)
	errs := make(chan error, 2)
	var wg sync.WaitGroup
	for _, worker := range []string{"w-pg-1", "w-pg-2"} {
		wg.Add(1)
		go func(w string) {
			defer wg.Done()
			actions, err := store.ClaimDue(ctx, w, 15, 200)
			results <- actions
			errs <- err
		}(worker)
	}
	wg.Wait()

	first := <-results
	second := <-results
	if err := <-errs; err != nil {
		t.Fatalf("claim w1: %v", err)
	}
	if err := <-errs; err != nil {
		t.Fatalf("claim w2: %v", err)
	}

	seen := map[string]bool{}
	for _, a := range append(append([]Action{}, first...), second...) {
		if seen[a.ID] {
			t.Errorf("action %s was claimed by BOTH workers on PostgreSQL — SKIP LOCKED failed", a.ID)
		}
		seen[a.ID] = true
	}
	if len(first)+len(second) != 30 {
		t.Errorf("claimed %d of 30 due actions on PostgreSQL", len(first)+len(second))
	}
}

// TestPostgresCompleteRemovesTheRow verifies retention on PostgreSQL.
func TestPostgresCompleteRemovesTheRow(t *testing.T) {
	store, _ := pgTestStore(t)
	ctx := context.Background()
	a := Action{
		ID:    ID("job-pg-comp", ActionAutoDelete, "-100222", 42),
		JobID: "job-pg-comp", Type: ActionAutoDelete,
		Transport: TransportUser, TransportRef: "sess-1",
		OwnerUserID: 77, ChatID: "-100222", MessageID: 42,
		DueAt: 100, NextAttemptAt: 100, MaxAttempts: 3,
	}
	if err := store.Schedule(ctx, a); err != nil {
		t.Fatalf("schedule: %v", err)
	}
	if err := store.Complete(ctx, a.ID); err != nil {
		t.Fatalf("complete: %v", err)
	}
	n, _ := store.PendingCount(ctx)
	if n != 0 {
		t.Errorf("pending after complete on PostgreSQL = %d, want 0", n)
	}
}

// TestPostgresRetryAndFailLifecycle verifies the retry budget on PostgreSQL.
func TestPostgresRetryAndFailLifecycle(t *testing.T) {
	store, _ := pgTestStore(t)
	ctx := context.Background()
	a := Action{
		ID:    ID("job-pg-ret", ActionAutoDelete, "-100222", 42),
		JobID: "job-pg-ret", Type: ActionAutoDelete,
		Transport: TransportUser, TransportRef: "sess-1",
		OwnerUserID: 77, ChatID: "-100222", MessageID: 42,
		DueAt: 100, NextAttemptAt: 100, MaxAttempts: 3,
	}
	if err := store.Schedule(ctx, a); err != nil {
		t.Fatalf("schedule: %v", err)
	}

	// 1. Retry once:
	if err := store.Retry(ctx, a.ID, 500, "transient error"); err != nil {
		t.Fatalf("retry: %v", err)
	}
	claimed, err := store.ClaimDue(ctx, "w1", 10, 600)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 1 || claimed[0].Attempts != 1 {
		t.Fatalf("claim after retry: attempts=%d, want 1", claimed[0].Attempts)
	}

	// 2. Fail permanently:
	if err := store.Fail(ctx, a.ID, "PERMISSION_DENIED"); err != nil {
		t.Fatalf("fail: %v", err)
	}
	claimedAgain, _ := store.ClaimDue(ctx, "w1", 10, 1000)
	for _, c := range claimedAgain {
		if c.ID == a.ID {
			t.Error("a FAILED action was claimed on PostgreSQL — terminal status failed")
		}
	}
}
