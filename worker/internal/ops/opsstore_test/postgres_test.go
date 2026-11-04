// Package opsstore_test pins the queue store aggregations the /ops surface
// reads through — kept in its own package because the ops package itself
// is imported by internal/queue (its store adapters), so an ops-package
// test importing queue would be an import cycle.
package opsstore_test

import (
	"context"
	"os"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/ops"
	"atf/worker/internal/queue"
)

// The PostgreSQL aggregation is the same contract as the SQLite one
// (parity is the whole point of the dual-backend requirement), so these
// tests mirror store_sqlite_test.go's fixture against a REAL postgres —
// never a SQLite stand-in — gated on the same env var the queue suite
// uses. Unset: skip, so a local `go test ./...` stays green without a DB.
const pgEnv = "ATF_TEST_POSTGRES_DSN"

func TestPostgresOpsAggregations(t *testing.T) {
	if os.Getenv(pgEnv) == "" {
		t.Skipf("%s not set; skipping real-PostgreSQL ops aggregation tests", pgEnv)
	}
	store := queue.NewPostgresTestStore(t)
	reader, ok := store.(ops.QueueStoreReader)
	if !ok {
		t.Fatalf("pgStore must satisfy ops.QueueStoreReader")
	}
	taskReader, ok := store.(ops.TaskStoreReader)
	if !ok {
		t.Fatalf("pgStore must satisfy ops.TaskStoreReader")
	}

	now := time.Now().Unix()
	// Every row needs a DISTINCT (rule, source, message, target, kind):
	// Enqueue's ON CONFLICT DO NOTHING is idempotent, and a fixture that
	// re-used the key would silently collapse to one row per rule.
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
		if _, err := store.Enqueue(context.Background(), j); err != nil {
			t.Fatalf("enqueue %s: %v", id, err)
		}
		// Enqueue writes PENDING; the fixture needs other statuses, and the
		// store's own state transitions are the honest way to reach them.
		db := queue.StoreDBForOps(store)
		if status != job.StatusPending {
			if _, err := db.ExecContext(context.Background(),
				`UPDATE delivery_jobs SET status=$1, attempts=$2, lease_until=$3, next_retry_at=$4, created_at=$5 WHERE id=$6`,
				string(status), attempts, lease, nextRetry, createdAt, id); err != nil {
				t.Fatalf("set status %s: %v", id, err)
			}
		}
	}

	liveLease := float64(now + 60)
	insert("a1", "ruleA", job.StatusPending, 0, 5, now-30, 0, 0)
	insert("a2", "ruleA", job.StatusPending, 0, 5, now-10, 0, 0)
	insert("a3", "ruleA", job.StatusClaimed, 0, 5, now-5, 0, liveLease)
	insert("a4", "ruleA", job.StatusSent, 1, 5, now-20, 0, 0)
	insert("a5", "ruleA", job.StatusFailed, 1, 5, now-15, 0, 0)
	insert("b1", "ruleB", job.StatusRetryWait, 1, 5, now-40, 0, 0)
	insert("b2", "ruleB", job.StatusRetryWait, 2, 5, now-25, float64(now+120), 0)
	insert("b3", "ruleB", job.StatusFailed, 5, 5, now-12, 0, 0)
	insert("c1", "ruleC", job.StatusSent, 1, 5, now-8, 0, 0)
	insert("c2", "ruleC", job.StatusUnknown, 1, 5, now-6, 0, 0)
	insert("d1", "ruleD", job.StatusPending, 0, 5, now-2, 0, 0)

	// --- the same assertions as the SQLite suite (parity is the contract) ---
	stats, err := reader.QueueStats(context.Background())
	if err != nil {
		t.Fatalf("QueueStats: %v", err)
	}
	if stats.Pending != 3 {
		t.Errorf("pending = %d, want 3", stats.Pending)
	}
	if stats.Processing != 1 {
		t.Errorf("processing = %d, want 1 (live lease only)", stats.Processing)
	}
	if stats.Delivered != 2 {
		t.Errorf("delivered = %d, want 2", stats.Delivered)
	}
	if stats.Failed != 1 {
		t.Errorf("failed = %d, want 1 (a5, not exhausted)", stats.Failed)
	}
	if stats.DeadLetter != 1 {
		t.Errorf("dead_letter = %d, want 1 (b3 exhausted)", stats.DeadLetter)
	}
	if stats.Delayed != 1 {
		t.Errorf("delayed = %d, want 1 (b2 future backoff)", stats.Delayed)
	}
	if stats.RetryPending != 1 {
		t.Errorf("retry_pending = %d, want 1 (b1 backoff elapsed)", stats.RetryPending)
	}
	if stats.Unknown != 1 {
		t.Errorf("unknown = %d, want 1 (c2)", stats.Unknown)
	}
	if stats.OldestPendingAgeSeconds < 25 || stats.OldestPendingAgeSeconds > 45 {
		t.Errorf("oldest_pending_age_seconds = %v, want ~30", stats.OldestPendingAgeSeconds)
	}

	rows, err := taskReader.TaskRows(context.Background(), 100)
	if err != nil {
		t.Fatalf("TaskRows: %v", err)
	}
	byRule := map[string]*ops.TaskRow{}
	for _, r := range rows {
		byRule[r.RuleID] = r
	}
	if len(byRule) != 4 {
		t.Fatalf("task rows = %d rules, want 4: %+v", len(byRule), byRule)
	}
	a := byRule["ruleA"]
	if a.Pending != 2 || a.Processing != 1 || a.Delivered != 1 || a.Failed != 1 {
		t.Errorf("ruleA aggregates = %+v", a)
	}
	if a.LastSuccessAt == "" {
		t.Error("ruleA last_success_at empty with a SENT row")
	}
	if byRule["ruleD"].Pending != 1 {
		t.Errorf("ruleD pending = %d, want 1", byRule["ruleD"].Pending)
	}
}
