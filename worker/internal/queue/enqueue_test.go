package queue

import (
	"context"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
)

// newTestStore builds the Core's queue schema in a temp SQLite. It is the
// same helper the other queue tests use (fixture.go), reached through the
// package's own constructor so this file stays in the queue package and
// can read the store's rows directly.
func enqueueTestStore(t *testing.T) Store {
	t.Helper()
	return newTestStore(t)
}

// TestEnqueueIsIdempotent pins the enqueue contract the keyword-reply
// deliverable relies on (Stage 4.5, W4): the same job enqueued twice
// leaves exactly one row, and the second call reports "already there"
// rather than an error.
func TestEnqueueIsIdempotent(t *testing.T) {
	store := enqueueTestStore(t)
	ctx := context.Background()

	j := &job.DeliveryJob{
		ID: "r1:c:1:t:keyword_reply", RuleID: "r1", SourceChatID: "c",
		SourceMessageID: 1, TargetChatID: "t", MaxAttempts: 5,
		JobKind: "keyword_reply",
		Payload: *job.NewPayload(map[string]any{
			"text": "thanks", "is_reply": true,
		}),
	}

	first, err := store.Enqueue(ctx, j)
	if err != nil {
		t.Fatalf("first enqueue: %v", err)
	}
	if !first {
		t.Fatal("first enqueue reported not inserted; the row did not exist")
	}

	// The duplicates the W4 block protects against: a replayed decision,
	// a re-claimed job, a crash between the send and the commit.
	for i := 0; i < 3; i++ {
		dup, err := store.Enqueue(ctx, j)
		if err != nil {
			t.Fatalf("duplicate enqueue %d: %v", i, err)
		}
		if dup {
			t.Fatalf("duplicate enqueue %d reported inserted; the row existed", i)
		}
	}

	count := 0
	if err := storeDB(t, store).QueryRow(
		`SELECT COUNT(*) FROM delivery_jobs WHERE id = ?`, j.ID).Scan(&count); err != nil {
		t.Fatalf("count rows: %v", err)
	}
	if count != 1 {
		t.Fatalf("rows = %d, want 1 (the enqueue must be idempotent)", count)
	}
}

// TestEnqueueKeepsTheKindDistinct pins why a reply and a forward for the
// same event are two rows and not one: the (rule, source, message,
// target, job_kind) UNIQUE key carries the kind, so the reply's enqueue
// never collides with the forward's (database.py:1177, durable_queue's
// own comment on the v20 widening).
func TestEnqueueKeepsTheKindDistinct(t *testing.T) {
	store := enqueueTestStore(t)
	ctx := context.Background()

	forward := &job.DeliveryJob{
		ID: "r1:c:2:t", RuleID: "r1", SourceChatID: "c",
		SourceMessageID: 2, TargetChatID: "t", MaxAttempts: 5,
		JobKind: "forward",
		Payload: *job.NewPayload(map[string]any{"text": "body"}),
	}
	reply := &job.DeliveryJob{
		ID: "r1:c:2:t:keyword_reply", RuleID: "r1", SourceChatID: "c",
		SourceMessageID: 2, TargetChatID: "t", MaxAttempts: 5,
		JobKind: "keyword_reply",
		Payload: *job.NewPayload(map[string]any{"text": "thanks", "is_reply": true}),
	}

	for _, j := range []*job.DeliveryJob{forward, reply} {
		inserted, err := store.Enqueue(ctx, j)
		if err != nil {
			t.Fatalf("enqueue %s: %v", j.ID, err)
		}
		if !inserted {
			t.Fatalf("enqueue %s reported not inserted", j.ID)
		}
	}

	count := 0
	if err := storeDB(t, store).QueryRow(
		`SELECT COUNT(*) FROM delivery_jobs
		 WHERE rule_id = 'r1' AND source_chat_id = 'c'
		   AND source_message_id = 2 AND target_chat_id = 't'`).Scan(&count); err != nil {
		t.Fatalf("count rows: %v", err)
	}
	if count != 2 {
		t.Fatalf("rows = %d, want 2 (a forward AND a reply for one event)", count)
	}
}

// TestEnqueuePersistsPENDINGWithAZeroAttemptBudget pins the row the
// claim loop will pick up: status PENDING, no spent attempts, no worker,
// and the payload verbatim.
func TestEnqueuePersistsPENDINGWithAZeroAttemptBudget(t *testing.T) {
	store := enqueueTestStore(t)
	ctx := context.Background()

	j := &job.DeliveryJob{
		ID: "r1:c:3:t", RuleID: "r1", SourceChatID: "c",
		SourceMessageID: 3, TargetChatID: "t", MaxAttempts: 5,
		JobKind: "forward",
		Payload: *job.NewPayload(map[string]any{"text": "body", "session_id": "s1"}),
	}
	if _, err := store.Enqueue(ctx, j); err != nil {
		t.Fatalf("enqueue: %v", err)
	}

	var (
		status   string
		attempts int
		worker   string
		kind     string
	)
	if err := storeDB(t, store).QueryRow(
		`SELECT status, attempts, worker_id, job_kind FROM delivery_jobs WHERE id = ?`,
		j.ID).Scan(&status, &attempts, &worker, &kind); err != nil {
		t.Fatalf("read row: %v", err)
	}
	if status != "PENDING" {
		t.Fatalf("status = %q, want PENDING", status)
	}
	if attempts != 0 {
		t.Fatalf("attempts = %d, want 0", attempts)
	}
	if worker != "" {
		t.Fatalf("worker_id = %q, want empty (unclaimed)", worker)
	}
	if kind != "forward" {
		t.Fatalf("job_kind = %q, want forward", kind)
	}

	// The enqueued row must be claimable like any other, which is the
	// whole point of scheduling a reply as a durable job.
	claimed, err := store.ClaimBatch(ctx, "w-enqueue", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 1 || claimed[0].ID != j.ID {
		t.Fatalf("claimed = %d jobs, want exactly the enqueued one", len(claimed))
	}
}

// TestEnqueueRefusesANilJob: the guard is an explicit error rather than a
// panic or a silently-written empty row.
func TestEnqueueRefusesANilJob(t *testing.T) {
	store := enqueueTestStore(t)
	if _, err := store.Enqueue(context.Background(), nil); err == nil {
		t.Fatal("Enqueue(nil) returned no error")
	}
}

// TestEnqueueDefaultsTheKind pins the row's DEFAULT value: a job built
// without an explicit kind enqueues as a forward, matching the column
// default (database.py:297).
func TestEnqueueDefaultsTheKind(t *testing.T) {
	store := enqueueTestStore(t)
	j := &job.DeliveryJob{
		ID: "r1:c:4:t", RuleID: "r1", SourceChatID: "c",
		SourceMessageID: 4, TargetChatID: "t", MaxAttempts: 5,
		Payload: *job.NewPayload(map[string]any{"text": "body"}),
	}
	if _, err := store.Enqueue(context.Background(), j); err != nil {
		t.Fatalf("enqueue: %v", err)
	}
	var kind string
	if err := storeDB(t, store).QueryRow(
		`SELECT job_kind FROM delivery_jobs WHERE id = ?`, j.ID).Scan(&kind); err != nil {
		t.Fatalf("read row: %v", err)
	}
	if kind != "forward" {
		t.Fatalf("job_kind = %q, want the default 'forward'", kind)
	}
}
