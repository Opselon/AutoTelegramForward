package queue

import (
	"context"
	"database/sql"
	"fmt"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
)

// schema is the subset of the Core's atf.db schema the queue touches. It is
// replayed here verbatim (the same column names and constraints the Core's
// SqliteDatabase._migrate creates) so the store is tested against the real
// shape rather than a hand-rolled approximation.


func mustParseJob(t *testing.T, raw map[string]any) *job.Payload {
	t.Helper()
	return job.NewPayload(raw)
}

func TestClaimBatchEmpty(t *testing.T) {
	s := newTestStore(t)
	got, err := s.ClaimBatch(context.Background(), "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim on empty queue: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("expected no jobs, got %d", len(got))
	}
}

func TestClaimBatchClaimsAndMarks(t *testing.T) {
	s := newTestStore(t)
	insertJob(t, s, &job.DeliveryJob{
		ID: "r1:-100123:42:-100456", RuleID: "r1",
		SourceChatID: "-100123", SourceMessageID: 42, TargetChatID: "-100456",
		Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *mustParseJob(t, map[string]any{"session_id": "s1", "text": "hello"}),
	})

	got, err := s.ClaimBatch(context.Background(), "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected 1 claimed job, got %d", len(got))
	}
	if got[0].Status != job.StatusClaimed {
		t.Errorf("status = %s, want CLAIMED", got[0].Status)
	}
	if got[0].WorkerID != "w1" {
		t.Errorf("worker = %s, want w1", got[0].WorkerID)
	}
	if got[0].Payload.SessionID() != "s1" {
		t.Errorf("payload session_id = %q, want s1", got[0].Payload.SessionID())
	}
	// A second claim must not double-claim while the lease is live.
	got2, err := s.ClaimBatch(context.Background(), "w2", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("second claim: %v", err)
	}
	if len(got2) != 0 {
		t.Fatalf("expected no re-claim, got %d", len(got2))
	}
}

// TestTwoWorkersOneLane is the single most important queue invariant: one
// target chat is one lane, with at most one in-flight job at a time. Two
// workers must never deliver to the same target concurrently.
func TestTwoWorkersOneLane(t *testing.T) {
	s := newTestStore(t)
	for i := 0; i < 3; i++ {
		insertJob(t, s, &job.DeliveryJob{
			ID: fmt.Sprintf("r1:-100123:%d:-100456", 40+i), RuleID: "r1",
			SourceChatID: "-100123", SourceMessageID: int64(40 + i), TargetChatID: "-100456",
			Status: job.StatusPending, MaxAttempts: 5, CreatedAt: int64(i),
		})
	}
	w1, err := s.ClaimBatch(context.Background(), "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("w1 claim: %v", err)
	}
	if len(w1) != 1 {
		t.Fatalf("lane must yield exactly one job to w1, got %d", len(w1))
	}
	// While w1's job is in flight, a second worker must see the lane blocked
	// even though two more PENDING jobs exist for the same target.
	w2, err := s.ClaimBatch(context.Background(), "w2", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("w2 claim: %v", err)
	}
	if len(w2) != 0 {
		t.Fatalf("lane isolation broken: w2 claimed %d job(s) for a busy target", len(w2))
	}
}

// TestLaneOrderPreserved checks the claim order: (source_chat_id,
// source_message_id, created_at, id). The source's own order is the message
// id; created_at only breaks ties inside one chat. Reordering by created_at
// alone was a real defect in the Python worker — the same rule applies here.
func TestLaneOrderPreserved(t *testing.T) {
	s := newTestStore(t)
	// Same chat: message ids 40, 41, 42 with created_at deliberately out of
	// order so only the source-message ordering can produce the right lane.
	insertJob(t, s, &job.DeliveryJob{
		ID: "r1:-100123:40:-100999", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 40, TargetChatID: "-100999", Status: job.StatusPending,
		MaxAttempts: 5, CreatedAt: 3,
	})
	insertJob(t, s, &job.DeliveryJob{
		ID: "r1:-100123:41:-100999", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 41, TargetChatID: "-100999", Status: job.StatusPending,
		MaxAttempts: 5, CreatedAt: 1,
	})
	got, err := s.ClaimBatch(context.Background(), "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(got) != 1 || got[0].SourceMessageID != 40 {
		t.Fatalf("expected lane head to be source message 40, got %+v", got)
	}
}

// TestDeferDoesNotSpendAttempt is the guarantee that separates a healthy
// forward from a dead-lettered one: a transient deferral (paused task,
// rate-limited target, offline session) must NOT consume one of the job's
// delivery attempts.
func TestDeferDoesNotSpendAttempt(t *testing.T) {
	s := newTestStore(t)
	insertJob(t, s, &job.DeliveryJob{
		ID: "r1:c:1:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
	})
	if err := s.Defer(context.Background(), "r1:c:1:t", "no live client", time.Millisecond); err != nil {
		t.Fatalf("defer: %v", err)
	}
	// The claim gate is next_retry_at: give it a moment so the deferred job
	// is due again (its RETRY_WAIT state is claimable once the wait elapses).
	time.Sleep(5 * time.Millisecond)
	got, err := s.ClaimBatch(context.Background(), "w1", 5, time.Second)
	if err != nil {
		t.Fatalf("claim after defer: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected the deferred job to become claimable again, got %d", len(got))
	}
	if got[0].Attempts != 0 {
		t.Errorf("defer spent an attempt: attempts = %d, want 0", got[0].Attempts)
	}
	if got[0].Status != job.StatusRetryWait && got[0].Status != job.StatusClaimed {
		t.Errorf("status = %s", got[0].Status)
	}
}

// TestDeferNeverReopensTerminal: a SENT or FAILED job must never be woken by
// a late deferral.
func TestDeferNeverReopensTerminal(t *testing.T) {
	s := newTestStore(t)
	for _, status := range []job.Status{job.StatusSent, job.StatusFailed} {
		insertJob(t, s, &job.DeliveryJob{
			ID: "r1:c:9:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 9,
			TargetChatID: "t", Status: status, MaxAttempts: 5,
		})
		err := s.Defer(context.Background(), "r1:c:9:t", "late defer", time.Second)
		if err != ErrNotFound {
			t.Errorf("defer on %s job: err = %v, want ErrNotFound", status, err)
		}
	}
}

// TestMarkRetrySpendsAttemptAndDeadLetters: mark_retry DOES spend an attempt
// and routes an exhausted budget to the dead-letter queue, replayable.
func TestMarkRetrySpendsAttemptAndDeadLetters(t *testing.T) {
	s := newTestStore(t)
	insertJob(t, s, &job.DeliveryJob{
		ID: "r1:c:2:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 2,
		TargetChatID: "t", Status: job.StatusClaimed, Attempts: 4, MaxAttempts: 5,
	})
	if err := s.MarkRetry(context.Background(), "r1:c:2:t", "transient", time.Second); err != nil {
		t.Fatalf("mark retry: %v", err)
	}
	// The last attempt (5 >= max 5) must have FAILED and dead-lettered.
	db := storeDB(t, s)
	var status string
	if err := db.QueryRow(`SELECT status FROM delivery_jobs WHERE id=?`, "r1:c:2:t").Scan(&status); err != nil {
		t.Fatalf("read status: %v", err)
	}
	if status != "FAILED" {
		t.Errorf("status = %s, want FAILED after budget exhaustion", status)
	}
	var dlCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM dead_letter_queue WHERE job_id=?`, "r1:c:2:t").Scan(&dlCount); err != nil {
		t.Fatalf("read dlq: %v", err)
	}
	if dlCount != 1 {
		t.Errorf("dead_letter rows = %d, want 1", dlCount)
	}
}

// TestFenceIsTheIdempotencyGuarantee: the send-side fence is what stops a
// lease expiry or a crash-recovery loop from double-dispatching. Only
// "dispatch" may send; SENT and UNKNOWN are never re-dispatched.
func TestFenceIsTheIdempotencyGuarantee(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{
		RuleID: "r1", SourceChatID: "c", SourceMessageID: 7,
		TargetChatID: "t", JobKind: "forward",
	}
	const id = "r1:c:7:t"

	// First claim: nothing exists yet → dispatch.
	outcome, _, err := s.FenceClaim(ctx, key, "w1", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence claim 1: %v", err)
	}
	if outcome != job.FenceDispatch {
		t.Fatalf("first claim outcome = %s, want dispatch", outcome)
	}

	// A second worker claiming while the first holds a live lease must NOT
	// be allowed to dispatch.
	outcome, _, err = s.FenceClaim(ctx, key, "w2", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence claim 2: %v", err)
	}
	if outcome != job.FenceInFlight {
		t.Fatalf("concurrent claim outcome = %s, want in_flight", outcome)
	}

	// Record the confirmed send, then verify the fence refuses a re-dispatch.
	if err := s.FenceRecordSent(ctx, key, "w1", 9911); err != nil {
		t.Fatalf("record sent: %v", err)
	}
	outcome, row, err := s.FenceClaim(ctx, key, "w3", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence claim after SENT: %v", err)
	}
	if outcome != job.FenceAlreadySent {
		t.Fatalf("claim after SENT outcome = %s, want already_sent", outcome)
	}
	if row == nil || row.TargetMessageID != 9911 {
		t.Fatalf("already_sent row = %+v, want the recorded target id", row)
	}

	_ = id
	// UNKNOWN: an earlier send whose outcome was lost. Never re-dispatched
	// automatically — reconciliation owns it.
	unknownKey := job.AttemptKey{
		RuleID: "r1", SourceChatID: "c", SourceMessageID: 8,
		TargetChatID: "t", JobKind: "forward",
	}
	db := storeDB(t, s)
	_, err = db.Exec(`INSERT INTO delivery_attempts
		(id, rule_id, source_chat_id, source_message_id, target_chat_id, job_kind,
		 status, worker_id, lease_until, attempts, last_error, created_at, updated_at)
		VALUES (?,?,?,?,?,?, 'UNKNOWN', 'w1', 0, 1, 'lost outcome', 0, 0)`,
		unknownKey.String(), "r1", "c", 8, "t", "forward")
	if err != nil {
		t.Fatalf("seed UNKNOWN fence: %v", err)
	}
	outcome, _, err = s.FenceClaim(ctx, unknownKey, "w9", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence claim UNKNOWN: %v", err)
	}
	if outcome != job.FenceUnknown {
		t.Fatalf("UNKNOWN claim outcome = %s, want unknown", outcome)
	}
	// The sanctioned reopen: only after the caller has PROVED no earlier
	// copy is in the target, with the reason recorded.
	if err := s.FenceResolveUnknown(ctx, unknownKey, "w9", "operator verified target empty"); err != nil {
		t.Fatalf("resolve unknown: %v", err)
	}
	outcome, _, err = s.FenceClaim(ctx, unknownKey, "w9", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence claim after resolve: %v", err)
	}
	if outcome != job.FenceDispatch {
		t.Fatalf("claim after resolve_unknown outcome = %s, want dispatch", outcome)
	}
}

// TestFenceExpiredClaimBecomesUnknownNotDispatch: a CLAIMED row whose lease
// passed moves to UNKNOWN, never back to dispatch-by-default — the send may
// have landed and re-dispatching is exactly the duplicate the fence exists
// to prevent.
func TestFenceExpiredClaimBecomesUnknownNotDispatch(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "c", SourceMessageID: 3, TargetChatID: "t"}
	_, _, err := s.FenceClaim(ctx, key, "w1", float64(time.Now().Add(-time.Minute).Unix())) // already expired
	if err != nil {
		t.Fatalf("fence claim: %v", err)
	}
	n, err := s.FenceExpireStale(ctx, float64(time.Now().Unix()))
	if err != nil {
		t.Fatalf("expire stale: %v", err)
	}
	if n != 1 {
		t.Fatalf("expired = %d, want 1", n)
	}
	db := storeDB(t, s)
	var status string
	if err := db.QueryRow(`SELECT status FROM delivery_attempts WHERE id=?`, key.String()).Scan(&status); err != nil {
		t.Fatalf("read: %v", err)
	}
	if status != "UNKNOWN" {
		t.Errorf("expired claim status = %s, want UNKNOWN", status)
	}
}

// TestMarkSentBatchCommitsOnce: a delivered batch commits with one UPDATE,
// and each id lands SENT.
func TestMarkSentBatchCommitsOnce(t *testing.T) {
	s := newTestStore(t)
	for i := 0; i < 3; i++ {
		insertJob(t, s, &job.DeliveryJob{
			ID: fmt.Sprintf("r1:c:%d:t", i), RuleID: "r1", SourceChatID: "c",
			SourceMessageID: int64(i), TargetChatID: "t", Status: job.StatusClaimed,
			MaxAttempts: 5,
		})
	}
	n, err := s.MarkSentBatch(context.Background(), []SentResult{
		{JobID: "r1:c:0:t", TargetMessageID: 100},
		{JobID: "r1:c:1:t", TargetMessageID: 101},
		{JobID: "r1:c:2:t", TargetMessageID: 102},
	})
	if err != nil {
		t.Fatalf("mark sent batch: %v", err)
	}
	if n != 3 {
		t.Errorf("updated = %d, want 3", n)
	}
	pending, err := s.PendingCount(context.Background())
	if err != nil {
		t.Fatalf("pending count: %v", err)
	}
	if pending != 0 {
		t.Errorf("pending = %d, want 0 after a full batch SENT", pending)
	}
}

// TestNoteMappingOnlyPrimaryForward: the mapping row belongs to the primary
// forward only. A keyword reply shares the map's key with the forward; let
// it write there and it replaces the forward's target id, so edit/delete
// sync would act on the reply's post instead of the forwarded copy.
func TestNoteMappingOnlyPrimaryForward(t *testing.T) {
	s := newTestStore(t)
	db := storeDB(t, s)
	_, err := db.Exec(`INSERT INTO message_map
		(rule_id, source_chat_id, source_message_id, target_chat_id, target_message_id, delivery_status, updated_at)
		VALUES ('r1','c',5,'t', NULL, 'PENDING', 0)`)
	if err != nil {
		t.Fatalf("seed map: %v", err)
	}
	// A keyword-reply job must NOT touch the forward's mapping row.
	reply := &job.DeliveryJob{
		ID: "r1:c:5:t:keyword_reply", RuleID: "r1", SourceChatID: "c",
		SourceMessageID: 5, TargetChatID: "t", JobKind: "keyword_reply",
		Payload: *job.NewPayload(map[string]any{"is_reply": true}),
	}
	if err := s.NoteMapping(context.Background(), reply, job.StatusSent, 777); err != nil {
		t.Fatalf("note mapping (reply): %v", err)
	}
	var targetID sql.NullInt64
	if err := db.QueryRow(`SELECT target_message_id FROM message_map WHERE rule_id='r1'`).Scan(&targetID); err != nil {
		t.Fatalf("read map: %v", err)
	}
	if targetID.Valid && targetID.Int64 == 777 {
		t.Error("keyword reply overwrote the forward's mapping target id")
	}
	// The primary forward DOES own the row.
	fwd := &job.DeliveryJob{
		ID: "r1:c:5:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 5,
		TargetChatID: "t", JobKind: "forward",
		Payload: *job.NewPayload(map[string]any{}),
	}
	if err := s.NoteMapping(context.Background(), fwd, job.StatusSent, 4242); err != nil {
		t.Fatalf("note mapping (forward): %v", err)
	}
	if err := db.QueryRow(`SELECT target_message_id FROM message_map WHERE rule_id='r1'`).Scan(&targetID); err != nil {
		t.Fatalf("read map: %v", err)
	}
	if !targetID.Valid || targetID.Int64 != 4242 {
		t.Errorf("forward mapping target id = %v, want 4242", targetID)
	}
}

// TestRecordAlbumProgressMergesAndPreserves: a split album's confirmed items
// are merged (never overwritten) and a terminal row is never rewritten —
// audit finding E2E-5: a retry used to re-send every item, duplicating the
// ones Telegram had already accepted.
func TestRecordAlbumProgressMergesAndPreserves(t *testing.T) {
	s := newTestStore(t)
	insertJob(t, s, &job.DeliveryJob{
		ID: "r1:c:6:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 6,
		TargetChatID: "t", Status: job.StatusClaimed, MaxAttempts: 5,
	})
	if err := s.RecordAlbumProgress(context.Background(), "r1:c:6:t", map[string]int64{"1": 11}); err != nil {
		t.Fatalf("record progress 1: %v", err)
	}
	if err := s.RecordAlbumProgress(context.Background(), "r1:c:6:t", map[string]int64{"2": 22}); err != nil {
		t.Fatalf("record progress 2: %v", err)
	}
	db := storeDB(t, s)
	var payloadJSON string
	if err := db.QueryRow(`SELECT payload_json FROM delivery_jobs WHERE id=?`, "r1:c:6:t").Scan(&payloadJSON); err != nil {
		t.Fatalf("read payload: %v", err)
	}
	for _, want := range []string{`"1":11`, `"2":22`} {
		if !contains(payloadJSON, want) {
			t.Errorf("payload %q missing %s", payloadJSON, want)
		}
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (haystack == needle ||
		(len(haystack) > 0 && containsStr(haystack, needle)))
}

func containsStr(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}


