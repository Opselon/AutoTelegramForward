// Package opsstore_test pins the queue store aggregations the /ops
// surface reads through — kept in its own package because the ops package
// itself is imported by internal/queue (its store adapters), so an
// ops-package test importing queue would be an import cycle.
package opsstore_test

// SQLite store aggregation tests: the /ops queue and per-task surfaces
// read through queue.Store's own methods, so the tests pin the numbers
// against a real SQLite store carrying the Core's queue schema.

import (
	"context"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/ops"
	"atf/worker/internal/queue"
)

// opsSQLiteStore builds a test store and seeds it with a controlled mix
// of job states — the exact fixture every assertion below reads.
type opsFixture struct {
	store queue.Store
}

func newOpsFixture(t *testing.T) *opsFixture {
	t.Helper()
	return &opsFixture{store: queue.NewTestStore(t)}
}

// seed writes the state mix the aggregations must report exactly.
//
//	ruleA: 2 PENDING, 1 CLAIMED (live lease), 1 SENT, 1 FAILED (not exhausted)
//	ruleB: 1 RETRY_WAIT (backoff elapsed), 1 FAILED exhausted (dead letter)
//	ruleC: 1 SENT, 1 UNKNOWN
//	ruleD: 1 PENDING only (the rule row exists: enabled)
func (f *opsFixture) seed(t *testing.T) {
	t.Helper()
	now := time.Now().Unix()
	n := 0
	insert := func(id, rule string, status job.Status, attempts, maxAttempts int, createdAt int64, nextRetry float64, lease float64) {
		n++
		j := &job.DeliveryJob{
			ID: id, RuleID: rule,
			SourceChatID: "-1001", SourceMessageID: int64(n), TargetChatID: "-2001",
			Status: status, Attempts: attempts, MaxAttempts: maxAttempts,
			NextRetryAt: nextRetry, LeaseUntil: lease,
			CreatedAt: createdAt, UpdatedAt: now,
			JobKind: "forward",
		}
		j.Payload = *job.NewPayload(map[string]any{"session_id": "s1"})
		queue.InsertJob(t, f.store, j)
	}
	liveLease := float64(now + 60)
	// ruleA
	insert("a1", "ruleA", job.StatusPending, 0, 5, now-30, 0, 0)
	insert("a2", "ruleA", job.StatusPending, 0, 5, now-10, 0, 0)
	insert("a3", "ruleA", job.StatusClaimed, 0, 5, now-5, 0, liveLease)
	insert("a4", "ruleA", job.StatusSent, 1, 5, now-20, 0, 0)
	insert("a5", "ruleA", job.StatusFailed, 1, 5, now-15, 0, 0)
	// ruleB
	insert("b1", "ruleB", job.StatusRetryWait, 1, 5, now-40, 0, 0) // backoff elapsed
	insert("b2", "ruleB", job.StatusRetryWait, 2, 5, now-25, float64(now+120), 0) // delayed
	insert("b3", "ruleB", job.StatusFailed, 5, 5, now-12, 0, 0)   // exhausted → dead letter
	// ruleC
	insert("c1", "ruleC", job.StatusSent, 1, 5, now-8, 0, 0)
	insert("c2", "ruleC", job.StatusUnknown, 1, 5, now-6, 0, 0)
	// ruleD
	insert("d1", "ruleD", job.StatusPending, 0, 5, now-2, 0, 0)
}

// TestSQLiteQueueStatusCounts pins the status-count breakdown against the
// seeded fixture: every category one rule composition drills into.
func TestSQLiteQueueStatusCounts(t *testing.T) {
	f := newOpsFixture(t)
	f.seed(t)

	reader, ok := f.store.(ops.QueueStoreReader)
	if !ok {
		t.Fatalf("sqliteStore must satisfy ops.ops.QueueStoreReader (it is required by /ops)")
	}
	stats, err := reader.QueueStats(context.Background())
	if err != nil {
		t.Fatalf("QueueStats: %v", err)
	}
	if stats.Pending != 3 {
		t.Errorf("pending = %d, want 3 (a1,a2,d1)", stats.Pending)
	}
	if stats.Processing != 1 { // a3 with a live lease
		t.Errorf("processing = %d, want 1 (a3 live lease)", stats.Processing)
	}
	if stats.Delivered != 2 { // a4, c1
		t.Errorf("delivered = %d, want 2 (a4,c1)", stats.Delivered)
	}
	if stats.Failed != 1 { // a5 only: failed, attempts < max
		t.Errorf("failed = %d, want 1 (a5)", stats.Failed)
	}
	if stats.DeadLetter != 1 { // b3: FAILED with attempts >= max_attempts
		t.Errorf("dead_letter = %d, want 1 (b3 exhausted)", stats.DeadLetter)
	}
	if stats.Delayed != 1 { // b2: RETRY_WAIT with a future next_retry_at
		t.Errorf("delayed = %d, want 1 (b2)", stats.Delayed)
	}
	if stats.RetryPending != 1 { // b1: RETRY_WAIT, backoff elapsed
		t.Errorf("retry_pending = %d, want 1 (b1)", stats.RetryPending)
	}
	if stats.Unknown != 1 { // c2
		t.Errorf("unknown = %d, want 1 (c2)", stats.Unknown)
	}
	// The oldest PENDING is a1 at now-30: the age is ~30s (the exact
	// number drifts with the test's own clock, so assert a range).
	if stats.OldestPendingAgeSeconds < 25 || stats.OldestPendingAgeSeconds > 45 {
		t.Errorf("oldest_pending_age_seconds = %v, want ~30", stats.OldestPendingAgeSeconds)
	}
}

// TestSQLiteTaskRows pins the per-task GROUP BY: counts per rule, with
// last_success/last_error drawn from the newest row of each status.
func TestSQLiteTaskRows(t *testing.T) {
	f := newOpsFixture(t)
	f.seed(t)

	reader, ok := f.store.(ops.TaskStoreReader)
	if !ok {
		t.Fatalf("sqliteStore must satisfy ops.TaskStoreReader")
	}
	rows, err := reader.TaskRows(context.Background(), 100)
	if err != nil {
		t.Fatalf("ops.TaskRows: %v", err)
	}
	byRule := map[string]*ops.TaskRow{}
	for _, r := range rows {
		byRule[r.RuleID] = r
	}
	if len(byRule) != 4 {
		t.Fatalf("task rows = %d rules, want 4 (ruleA..ruleD): %+v", len(byRule), byRule)
	}
	a := byRule["ruleA"]
	if a.Pending != 2 || a.Processing != 1 || a.Delivered != 1 || a.Failed != 1 {
		t.Errorf("ruleA aggregates = %+v", a)
	}
	// ruleA has one SENT row, so a last_success_at must be present.
	if a.LastSuccessAt == "" {
		t.Error("ruleA last_success_at is empty with a SENT row present")
	}
	b := byRule["ruleB"]
	if b.Pending != 0 || b.Failed != 1 {
		t.Errorf("ruleB aggregates = %+v", b)
	}
	// ruleD: one PENDING, nothing else.
	d := byRule["ruleD"]
	if d.Pending != 1 || d.Delivered != 0 {
		t.Errorf("ruleD aggregates = %+v", d)
	}
	// No forward_rules rows in the queue fixture: the join in the server
	// layer (not the store) fills source/target/enabled, and its absence is
	// reported as enabled=false — pinned in the server test.
}
