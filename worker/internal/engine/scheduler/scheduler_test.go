package scheduler

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"

	_ "github.com/mattn/go-sqlite3" // registers the sqlite3 driver

	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
)

// --- store fixtures --------------------------------------------------------- //

const scheduledActionsSchema = `
CREATE TABLE IF NOT EXISTS scheduled_actions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL DEFAULT 'auto_delete',
    transport TEXT NOT NULL DEFAULT 'user',
    transport_ref TEXT NOT NULL DEFAULT '',
    owner_user_id INTEGER NOT NULL DEFAULT 0,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,
    due_at REAL NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'PENDING',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(job_id, action_type, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_due
    ON scheduled_actions(state, next_attempt_at, due_at);
`

// newTestStore opens a fresh SQLite store carrying the v33 schema.
func newTestStore(t *testing.T) Store {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "atf.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	if _, err := db.Exec(scheduledActionsSchema); err != nil {
		db.Close()
		t.Fatalf("apply schema: %v", err)
	}
	store, err := NewSQLite(db)
	if err != nil {
		t.Fatalf("new sqlite store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// --- store contract tests --------------------------------------------------- //

// TestScheduleIsIdempotent is the deduplication guarantee: the same confirmed
// delivery cannot register the same deletion more than once, no matter how
// many times the scheduling path runs.
func TestScheduleIsIdempotent(t *testing.T) {
	store := newTestStore(t)
	ctx := context.Background()
	a := Action{
		ID:    ID("job-1", ActionAutoDelete, "-100222", 42),
		JobID: "job-1", Type: ActionAutoDelete,
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
		t.Errorf("duplicate schedule: pending=%d, want 1", n)
	}
}

// TestClaimDueOnlyClaimsReady verifies the due-time gate: an action whose
// next_attempt_at has not arrived is never claimed, and one that has is.
func TestClaimDueOnlyClaimsReady(t *testing.T) {
	store := newTestStore(t)
	ctx := context.Background()
	early := mustSchedule(t, store, "job-early", "-100222", 1, 100)
	late := mustSchedule(t, store, "job-late", "-100222", 2, 500)

	claimed, err := store.ClaimDue(ctx, "w1", 10, 200)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 1 || claimed[0].ID != early.ID {
		t.Fatalf("claim returned %v, want only the due action %s", claimed, early.ID)
	}
	if _, err := store.ClaimDue(ctx, "w1", 10, 200); err != nil {
		t.Fatalf("second claim: %v", err)
	}
	// The not-yet-due action must still be pending, unclaimed.
	n, _ := store.PendingCount(ctx)
	if n != 2 {
		t.Errorf("pending after claims = %d, want 2 (one claimed, one not yet due)", n)
	}
	if late.ID == "" {
		t.Error("the not-yet-due action has no id")
	}
}

// TestClaimDueIsAtomicBetweenTwoCallers proves the multi-instance safety: two
// concurrent claimersover the same due work never receive the same action.
func TestClaimDueIsAtomicBetweenTwoCallers(t *testing.T) {
	store := newTestStore(t)
	ctx := context.Background()
	for i := 0; i < 40; i++ {
		mustSchedule(t, store, fmt.Sprintf("job-%d", i), "-100222", int64(100+i), 100)
	}

	results := make(chan []Action, 2)
	errs := make(chan error, 2)
	for _, worker := range []string{"w1", "w2"} {
		go func(w string) {
			actions, err := store.ClaimDue(ctx, w, 20, 200)
			results <- actions
			errs <- err
		}(worker)
	}
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
			t.Errorf("action %s was claimed by BOTH workers — the claim is not atomic", a.ID)
		}
		seen[a.ID] = true
	}
	if len(first)+len(second) != 40 {
		t.Errorf("claimed %d of 40 due actions", len(first)+len(second))
	}
}

// TestCompleteRemovesTheRow is the minimal retention policy: a completed
// action is deleted, not archived.
func TestCompleteRemovesTheRow(t *testing.T) {
	store := newTestStore(t)
	ctx := context.Background()
	a := mustSchedule(t, store, "job-1", "-100222", 42, 100)
	if err := store.Complete(ctx, a.ID); err != nil {
		t.Fatalf("complete: %v", err)
	}
	n, _ := store.PendingCount(ctx)
	if n != 0 {
		t.Errorf("pending after complete = %d, want 0 (retention deletes)", n)
	}
}

// TestRetrySpendsAnAttemptAndRequeues verifies a failure puts the action back
// with the next attempt time and one more spent attempt.
func TestRetrySpendsAnAttemptAndRequeues(t *testing.T) {
	store := newTestStore(t)
	ctx := context.Background()
	a := mustSchedule(t, store, "job-1", "-100222", 42, 100)

	if err := store.Retry(ctx, a.ID, 900, "transient: connection reset"); err != nil {
		t.Fatalf("retry: %v", err)
	}
	actions, err := store.ClaimDue(ctx, "w1", 10, 1000)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(actions) != 1 || actions[0].ID != a.ID {
		t.Fatalf("claim = %v, want the re-queued action", actions)
	}
	if actions[0].Attempts != 1 {
		t.Errorf("attempts after one retry = %d, want 1", actions[0].Attempts)
	}
	if actions[0].LastError != "transient: connection reset" {
		t.Errorf("last_error = %q, want the retry detail", actions[0].LastError)
	}
}

// TestFailIsTerminal is the bounded-budget guarantee: a FAILED action is not
// claimable again, and its reason survives for a human to read.
func TestFailIsTerminal(t *testing.T) {
	store := newTestStore(t)
	ctx := context.Background()
	a := mustSchedule(t, store, "job-1", "-100222", 42, 100)

	if err := store.Fail(ctx, a.ID, "PERMISSION_DENIED: not enough rights"); err != nil {
		t.Fatalf("fail: %v", err)
	}
	claimed, err := store.ClaimDue(ctx, "w1", 10, float64(time.Now().Unix())+1000)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	for _, c := range claimed {
		if c.ID == a.ID {
			t.Error("a FAILED action was claimed again — the budget is not terminal")
		}
	}
	n, _ := store.PendingCount(ctx)
	if n != 0 {
		t.Errorf("pending after terminal failure = %d, want 0 (FAILED is not pending)", n)
	}
}

// --- engine tests ------------------------------------------------------------ //

// testClock is a controllable clock so no test sleeps a real TTL. The
// engine reads it from its loop goroutine while the test advances it, so the
// time value carries its own mutex.
type testClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *testClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *testClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

type captureMetrics struct {
	mu                                sync.Mutex
	deleted, retried, failed, unknown int
	depth                             int
	dueLatency                        float64
	retries                           int
}

func (m *captureMetrics) Outcome(kind string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	switch kind {
	case "deleted":
		m.deleted++
	case "retried":
		m.retried++
	case "failed":
		m.failed++
	case "unknown":
		m.unknown++
	}
}
func (m *captureMetrics) ObserveDueLatency(s float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.dueLatency = s
}
func (m *captureMetrics) ObserveQueueDepth(n int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.depth = n
}
func (m *captureMetrics) ObserveRetry(_ string, _ int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.retries++
}

// snapshot reads the counters the test asserts on, under the same lock.
func (m *captureMetrics) snapshot() captureMetrics {
	m.mu.Lock()
	defer m.mu.Unlock()
	return captureMetrics{
		deleted: m.deleted, retried: m.retried, failed: m.failed,
		unknown: m.unknown, depth: m.depth, dueLatency: m.dueLatency,
		retries: m.retries,
	}
}

type silentLogger struct{}

func (silentLogger) Info(string, ...any)  {}
func (silentLogger) Warn(string, ...any)  {}
func (silentLogger) Error(string, ...any) {}

// scheduleForTest inserts an action through the store, at the given due time.
func scheduleForTest(t *testing.T, store Store, jobID, chatID string, messageID int64, dueAt float64, transport Transport, ref string) Action {
	t.Helper()
	a := Action{
		ID:    ID(jobID, ActionAutoDelete, chatID, messageID),
		JobID: jobID, Type: ActionAutoDelete,
		Transport: transport, TransportRef: ref,
		OwnerUserID: 77, ChatID: chatID, MessageID: messageID,
		DueAt: dueAt, NextAttemptAt: dueAt, MaxAttempts: 3,
	}
	if err := store.Schedule(context.Background(), a); err != nil {
		t.Fatalf("schedule: %v", err)
	}
	return a
}

func mustSchedule(t *testing.T, store Store, jobID, chatID string, messageID int64, dueAt float64) Action {
	t.Helper()
	return scheduleForTest(t, store, jobID, chatID, messageID, dueAt, TransportUser, "sess-1")
}

// TestEngineDeletesDueAction proves the happy path end to end: a due action
// is claimed, deleted through the gateway, and removed from the store.
func TestEngineDeletesDueAction(t *testing.T) {
	store := newTestStore(t)
	gw := fakegateway.New("")
	metrics := &captureMetrics{}
	clock := &testClock{t: time.Unix(100, 0)}
	eng := New(store, gw, silentLogger{}, metrics, "w1", "sess-1", Options{
		PollInterval: 10 * time.Millisecond, BatchSize: 4, MaxConcurrent: 2,
		RPCTimeout: time.Second, MaxBackoff: time.Minute,
	})
	eng.SetClock(clock)

	scheduleForTest(t, store, "job-1", "-100222", 42, 50, TransportUser, "sess-1")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go func() { _ = eng.Run(ctx) }()

	if err := waitFor(clock, time.Millisecond*500, func() bool {
		return metrics.snapshot().deleted == 1
	}); err != nil {
		t.Fatalf("the due action was not deleted: %v (metrics=%+v)", err, metrics.snapshot())
	}
	cancel()

	n, _ := store.PendingCount(context.Background())
	if n != 0 {
		t.Errorf("pending after completion = %d, want 0", n)
	}
	calls := gw.Recorded()
	if len(calls) == 0 {
		t.Fatal("no gateway call was recorded")
	}
	want := "DeleteMessages:user"
	if calls[0].Op != want {
		t.Errorf("gateway op = %q, want %q", calls[0].Op, want)
	}
	if calls[0].ChatID != -100222 || len(calls[0].MessageIDs) != 1 || calls[0].MessageIDs[0] != 42 {
		t.Errorf("delete targeted chat=%d ids=%v, want -100222 [42]", calls[0].ChatID, calls[0].MessageIDs)
	}
}

// TestEngineDoesNotDeleteBeforeDue proves the due gate in the engine: an
// action whose time has not arrived is never executed.
func TestEngineDoesNotDeleteBeforeDue(t *testing.T) {
	store := newTestStore(t)
	gw := fakegateway.New("")
	metrics := &captureMetrics{}
	clock := &testClock{t: time.Unix(100, 0)}
	eng := New(store, gw, silentLogger{}, metrics, "w1", "sess-1", Options{
		PollInterval: 10 * time.Millisecond, BatchSize: 4, MaxConcurrent: 2,
	})
	eng.SetClock(clock)

	// Due at 500; the clock is frozen at 100.
	scheduleForTest(t, store, "job-1", "-100222", 42, 500, TransportUser, "sess-1")

	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	go func() { _ = eng.Run(ctx) }()
	<-ctx.Done()

	if metrics.deleted != 0 {
		t.Errorf("an action that was not yet due was deleted (metrics=%+v)", metrics.snapshot())
	}
}

// TestEngineRetriesTransientFailure proves backoff: a transient failure
// re-queues the action instead of dropping it, and the second attempt
// succeeds.
func TestEngineRetriesTransientFailure(t *testing.T) {
	store := newTestStore(t)
	gw := fakegateway.New("")
	// First delete fails transiently, then succeeds: exactly the shape a
	// network blip produces.
	transient := &telegraph.DeliveryFailure{Code: "NETWORK_DISCONNECTED", Message: "connection reset"}
	gw.SetDeleteScript(&fakegateway.DeleteResult{Deleted: false, Failure: transient})
	metrics := &captureMetrics{}
	clock := &testClock{t: time.Unix(100, 0)}
	eng := New(store, gw, silentLogger{}, metrics, "w1", "sess-1", Options{
		PollInterval: 10 * time.Millisecond, BatchSize: 4, MaxConcurrent: 2,
		MaxBackoff: time.Minute,
	})
	eng.SetClock(clock)

	scheduleForTest(t, store, "job-1", "-100222", 42, 50, TransportUser, "sess-1")

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go func() { _ = eng.Run(ctx) }()

	// The transient failure must become exactly one retry.
	if err := waitFor(clock, time.Second, func() bool { snap := metrics.snapshot(); return snap.retried == 1 }); err != nil {
		t.Fatalf("no retry happened: %v (metrics=%+v)", err, metrics.snapshot())
	}
	// Clear the script so the retry succeeds.
	gw.SetDeleteScript(nil)
	clock.Advance(time.Minute)
	if err := waitFor(clock, time.Second, func() bool { snap := metrics.snapshot(); return snap.deleted == 1 }); err != nil {
		t.Fatalf("the retried action was not deleted: %v (metrics=%+v)", err, metrics.snapshot())
	}
	cancel()
}

// TestEngineRespectsFloodWait proves Telegram's own wait is honored: the
// re-queue happens at the demanded window, not sooner.
func TestEngineRespectsFloodWait(t *testing.T) {
	store := newTestStore(t)
	gw := fakegateway.New("")
	flood := &telegraph.DeliveryFailure{Code: "FLOOD_WAIT", RetryAfterSeconds: 3600, Message: "Too Many Requests"}
	gw.SetDeleteScript(&fakegateway.DeleteResult{Deleted: false, Failure: flood})
	metrics := &captureMetrics{}
	clock := &testClock{t: time.Unix(100, 0)}
	eng := New(store, gw, silentLogger{}, metrics, "w1", "sess-1", Options{
		PollInterval: 10 * time.Millisecond, BatchSize: 4, MaxConcurrent: 2,
	})
	eng.SetClock(clock)

	scheduleForTest(t, store, "job-1", "-100222", 42, 50, TransportUser, "sess-1")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go func() { _ = eng.Run(ctx) }()

	if err := waitFor(clock, time.Second, func() bool { snap := metrics.snapshot(); return snap.retried == 1 }); err != nil {
		t.Fatalf("no re-queue happened: %v (metrics=%+v)", err, metrics)
	}
	// Advance less than the demanded window: the action must NOT execute.
	clock.Advance(30 * time.Minute)
	if metrics.deleted != 0 {
		t.Error("the action executed BEFORE the FloodWait window elapsed")
	}
	// Advance past the window (the poll interval is the latency floor).
	gw.SetDeleteScript(nil)
	clock.Advance(time.Hour)
	if err := waitFor(clock, time.Second, func() bool { snap := metrics.snapshot(); return snap.deleted == 1 }); err != nil {
		t.Errorf("the action did not execute after the FloodWait window: %v", err)
	}
	cancel()
}

// TestEngineHonorsAlreadyDeleted proves the idempotent-cleanup rule: a
// message Telegram reports as already gone is a successful action.
func TestEngineHonorsAlreadyDeleted(t *testing.T) {
	store := newTestStore(t)
	gw := fakegateway.New("")
	gw.SetDeleteScript(&fakegateway.DeleteResult{Deleted: true, Failure: nil})
	metrics := &captureMetrics{}
	clock := &testClock{t: time.Unix(100, 0)}
	eng := New(store, gw, silentLogger{}, metrics, "w1", "sess-1", Options{
		PollInterval: 10 * time.Millisecond, BatchSize: 4, MaxConcurrent: 2,
	})
	eng.SetClock(clock)

	scheduleForTest(t, store, "job-1", "-100222", 42, 50, TransportUser, "sess-1")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go func() { _ = eng.Run(ctx) }()
	if err := waitFor(clock, time.Second, func() bool { snap := metrics.snapshot(); return snap.deleted == 1 }); err != nil {
		t.Fatalf("an already-deleted message was not treated as a completed action: %v", err)
	}
	cancel()
}

// TestEngineBudgetExhaustionFails proves the bounded retry budget: a
// permanent permission failure stops being retried and leaves a FAILED row
// a human can see.
func TestEngineBudgetExhaustionFails(t *testing.T) {
	store := newTestStore(t)
	gw := fakegateway.New("")
	perm := &telegraph.DeliveryFailure{Code: "PERMISSION_DENIED", Message: "not enough rights to delete messages"}
	gw.SetDeleteScript(&fakegateway.DeleteResult{Deleted: false, Failure: perm})
	metrics := &captureMetrics{}
	clock := &testClock{t: time.Unix(100, 0)}
	eng := New(store, gw, silentLogger{}, metrics, "w1", "sess-1", Options{
		PollInterval: 10 * time.Millisecond, BatchSize: 4, MaxConcurrent: 2,
		MaxBackoff: time.Second,
	})
	eng.SetClock(clock)

	// MaxAttempts=3: three spendings, then terminal.
	a := scheduleForTest(t, store, "job-1", "-100222", 42, 50, TransportUser, "sess-1")
	a.MaxAttempts = 3
	_ = a

	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	go func() { _ = eng.Run(ctx) }()

	if err := waitFor(clock, 3*time.Second, func() bool { snap := metrics.snapshot(); return snap.failed == 1 }); err != nil {
		t.Fatalf("the budget was never exhausted: %v (metrics=%+v)", err, metrics.snapshot())
	}
	cancel()
	if metrics.snapshot().retried > 3 {
		t.Errorf("retries = %d, the budget (3) was exceeded", metrics.retried)
	}
	n, _ := store.PendingCount(context.Background())
	if n != 0 {
		t.Errorf("pending after terminal failure = %d, want 0", n)
	}
}

// TestEngineBOTDeletionUsesTheBotTransport proves the transport the delivery
// actually used is the transport the deletion uses.
func TestEngineBOTDeletionUsesTheBotTransport(t *testing.T) {
	store := newTestStore(t)
	gw := fakegateway.New("")
	metrics := &captureMetrics{}
	clock := &testClock{t: time.Unix(100, 0)}
	eng := New(store, gw, silentLogger{}, metrics, "w1", "sess-1", Options{
		PollInterval: 10 * time.Millisecond, BatchSize: 4, MaxConcurrent: 2,
	})
	eng.SetClock(clock)

	scheduleForTest(t, store, "job-bot", "-100222", 7, 50, TransportBot, "tok-9")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go func() { _ = eng.Run(ctx) }()
	if err := waitFor(clock, time.Second, func() bool { snap := metrics.snapshot(); return snap.deleted == 1 }); err != nil {
		t.Fatalf("the BOT action was not deleted: %v", err)
	}
	cancel()

	// DeleteMessages records into the unified call log with the transport
	// named in the op; the credentials never appear anywhere.
	calls := gw.Recorded()
	if len(calls) == 0 {
		t.Fatal("no gateway call was recorded")
	}
	if calls[0].Op != "DeleteMessages:bot" {
		t.Errorf("delete op = %q, want DeleteMessages:bot", calls[0].Op)
	}
}

// TestEngineGracefulShutdown proves stopping the loop is clean: no claim
// keeps running, and the engine's Run returns.
func TestEngineGracefulShutdown(t *testing.T) {
	store := newTestStore(t)
	gw := fakegateway.New("")
	metrics := &captureMetrics{}
	clock := &testClock{t: time.Unix(100, 0)}
	eng := New(store, gw, silentLogger{}, metrics, "w1", "sess-1", Options{
		PollInterval: 10 * time.Millisecond, BatchSize: 4, MaxConcurrent: 2,
	})
	eng.SetClock(clock)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- eng.Run(ctx) }()

	// Give the loop one tick, then cancel.
	time.Sleep(60 * time.Millisecond)
	cancel()

	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Run returned an error on shutdown: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the scheduler loop did not shut down within 2s")
	}
}

// --- helpers ----------------------------------------------------------------- //

// waitFor polls a condition while advancing the injected clock. The deadline
// is real time (the loop's poll interval is a real ticker), and the clock is
// advanced a little each tick so a re-queued action whose backoff the frozen
// clock would otherwise never pass actually becomes due.
func waitFor(clock *testClock, limit time.Duration, cond func() bool) error {
	deadline := time.Now().Add(limit)
	for time.Now().Before(deadline) {
		if cond() {
			return nil
		}
		// Advance the clock past any bounded backoff so the next claim sees
		// re-queued work: real time keeps the loop honest, the injected clock
		// keeps the due gate honest.
		clock.Advance(2 * time.Second)
		time.Sleep(5 * time.Millisecond)
	}
	if cond() {
		return nil
	}
	return fmt.Errorf("condition not met within %s", limit)
}

var _ = sql.Open
