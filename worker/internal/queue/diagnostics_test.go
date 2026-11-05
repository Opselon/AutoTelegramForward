package queue

import (
	"context"
	"testing"
	"time"

	"atf/worker/internal/ops"
)

// The diagnostics read is the §5 "is the queue broken, or just busy?" answer.
// Each case below pins one signal against the real delivery_jobs schema via
// the package's own test fixtures, so the query cannot silently drift from
// production columns the way a hand-rolled mock could.

// insertDiagnosticsRow writes a delivery_jobs row with the exact fields the
// diagnostics query reads: status, attempts/max_attempts, lease_until,
// next_retry_at, created_at, error_detail.
func insertDiagnosticsRow(t *testing.T, s Store, id, status string, attempts, maxAttempts int, leaseUntil, nextRetry, createdAt float64, errorDetail string) {
	t.Helper()
	db := storeDB(t, s)
	_, err := db.ExecContext(context.Background(), `
		INSERT INTO delivery_jobs (id, rule_id, source_chat_id, source_message_id, target_chat_id,
		                           payload_json, status, attempts, max_attempts, next_retry_at,
		                           lease_until, worker_id, error_detail, created_at, updated_at)
		VALUES (?, 'r', '-100', 1, '-200', '{}', ?, ?, ?, ?, ?, 'w', ?, ?, ?)`,
		id, status, attempts, maxAttempts, nextRetry, leaseUntil, errorDetail, createdAt, createdAt)
	if err != nil {
		t.Fatalf("insert %s: %v", id, err)
	}
}

// TestQueueDiagnosticsStuckClaimed: a CLAIMED row whose lease has expired
// counts as stuck — a worker that died mid-job leaves exactly this behind,
// and the whole point of the signal is to make that visible without reading
// the log. A live lease must NOT count.
func TestQueueDiagnosticsStuckClaimed(t *testing.T) {
	s := newTestStore(t)
	now := time.Now().Unix()
	insertDiagnosticsRow(t, s, "stuck", "CLAIMED", 1, 5, float64(now-120), 0, float64(now-200), "")
	insertDiagnosticsRow(t, s, "live", "CLAIMED", 1, 5, float64(now+30), 0, float64(now-10), "")

	diag, err := s.QueueDiagnostics(context.Background(), now)
	if err != nil {
		t.Fatalf("QueueDiagnostics: %v", err)
	}
	if diag.StuckClaimed != 1 {
		t.Errorf("stuck_claimed = %d, want 1 (only the expired lease)", diag.StuckClaimed)
	}
}

// TestQueueDiagnosticsStuckPending: an old, still-unclaimed PENDING row is
// queue starvation — the claim loop is not running or cannot keep up. A
// fresh pending row must NOT count: a busy queue is not a broken one.
func TestQueueDiagnosticsStuckPending(t *testing.T) {
	s := newTestStore(t)
	now := time.Now().Unix()
	insertDiagnosticsRow(t, s, "old", "PENDING", 0, 5, 0, 0, float64(now-600), "")
	insertDiagnosticsRow(t, s, "fresh", "PENDING", 0, 5, 0, 0, float64(now-3), "")

	diag, err := s.QueueDiagnostics(context.Background(), now)
	if err != nil {
		t.Fatalf("QueueDiagnostics: %v", err)
	}
	if diag.StuckPending != 1 {
		t.Errorf("stuck_pending = %d, want 1 (only the 10-minute-old row)", diag.StuckPending)
	}
	if diag.OldestPendingAgeSeconds < 500 {
		t.Errorf("oldest_pending_age = %.0f, want ~600", diag.OldestPendingAgeSeconds)
	}
}

// TestQueueDiagnosticsPoisonAndRetryStorm: a job that has burned its whole
// retry budget on a PERMANENT error is poison — the queue is spending
// capacity on a delivery that can never succeed, and it belongs in the
// dead-letter conversation, not the active backlog. A RETRY_WAIT whose
// backoff lands inside the next minute is the leading edge of a retry storm.
func TestQueueDiagnosticsPoisonAndRetryStorm(t *testing.T) {
	s := newTestStore(t)
	now := time.Now().Unix()
	insertDiagnosticsRow(t, s, "poison", "FAILED", 5, 5, 0, 0, float64(now-100), "permanent: chat forwards restricted")
	// Same budget, but a retryable error: NOT poison.
	insertDiagnosticsRow(t, s, "retryable", "FAILED", 5, 5, 0, 0, float64(now-100), "timeout")
	insertDiagnosticsRow(t, s, "soon", "RETRY_WAIT", 2, 5, 0, float64(now+10), float64(now-50), "")
	insertDiagnosticsRow(t, s, "later", "RETRY_WAIT", 2, 5, 0, float64(now+600), float64(now-50), "")

	diag, err := s.QueueDiagnostics(context.Background(), now)
	if err != nil {
		t.Fatalf("QueueDiagnostics: %v", err)
	}
	if diag.PoisonJobs != 1 {
		t.Errorf("poison_jobs = %d, want 1 (only the permanent error at max attempts)", diag.PoisonJobs)
	}
	if diag.RetryStorm != 1 {
		t.Errorf("retry_storm = %d, want 1 (only the retry inside the next 60s)", diag.RetryStorm)
	}
}

// TestQueueDiagnosticsEmpty: an empty queue reports zeros, not NULLs — the
// console renders "no backlog" from the numbers, and a NULL would arrive as
// a missing field that reads as "unknown".
func TestQueueDiagnosticsEmpty(t *testing.T) {
	s := newTestStore(t)
	diag, err := s.QueueDiagnostics(context.Background(), time.Now().Unix())
	if err != nil {
		t.Fatalf("QueueDiagnostics: %v", err)
	}
	if diag.StuckClaimed != 0 || diag.StuckPending != 0 || diag.PoisonJobs != 0 || diag.RetryStorm != 0 {
		t.Errorf("empty queue = %+v, want all zeros", diag)
	}
}

// TestNewQueueDiagnosticsThresholds: the snapshot must carry the thresholds
// it was computed with, so the UI never has to guess what "stuck" meant.
// A store that does not implement the interface reports UNAVAILABLE, not
// fabricated zeros.
func TestNewQueueDiagnosticsThresholds(t *testing.T) {
	ctx := context.Background()
	s := newTestStore(t)
	diag, err := ops.NewQueueDiagnostics(ctx, s, time.Now().Unix())
	if err != nil {
		t.Fatalf("NewQueueDiagnostics: %v", err)
	}
	if diag == nil {
		t.Fatal("diagnostics = nil for a store that implements the interface")
	}
	if diag.StuckClaimedThresholdSeconds != ops.StuckClaimedThreshold ||
		diag.StuckPendingThresholdSeconds != ops.StuckPendingThreshold ||
		diag.RetryStormWindowSeconds != ops.RetryStormWindow {
		t.Errorf("thresholds not stamped: %+v", diag)
	}

	// A value that does NOT implement the interface returns nil, nil —
	// unavailable, never fabricated zeros.
	legacy := noDiagnostician{}
	diag2, err := ops.NewQueueDiagnostics(ctx, legacy, time.Now().Unix())
	if err != nil {
		t.Fatalf("NewQueueDiagnostics(legacy): %v", err)
	}
	if diag2 != nil {
		t.Errorf("diagnostics = %+v for a non-implementing store, want nil (unavailable)", diag2)
	}
}

// noDiagnostician is a Store-shaped value that does NOT implement
// ops.QueueDiagnostician — the legacy-backend case.
type noDiagnostician struct{}
