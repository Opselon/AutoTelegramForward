package queue

// guarantees_test.go — the Stage 5 delivery-guarantee suite, at the store
// level, on BOTH backends.
//
// Each test here pins one of the seven guarantees the production cutover
// depends on, against the real SQL of the real engine:
//
//  1. Idempotency — a duplicate Enqueue (same UNIQUE key, job_kind included)
//     is a no-op, never a second delivery.
//  2. Ambiguous outcome — a send that produced no deterministic id is
//     MarkUnknown, never retried and never SENT.
//  3. Worker crash — a lease that expired without completion returns the job
//     to the queue, and the fence stops the re-claim from double-sending.
//  4. Partial multi-target failure — one target's failure does not touch the
//     other target's recorded success.
//  5. Album delivery integrity — album progress checkpoints merge and survive.
//  6. Edit sync — the fence keys the edit onto the original event, so the
//     edit collapses onto the confirmed send instead of duplicating it.
//  7. Rate limit — Defer postpones without spending an attempt and releases
//     the target lane.
//
// The SQLite half runs on every `go test ./...` (the temp-file store needs
// no server). The PostgreSQL half runs only with ATF_TEST_POSTGRES_DSN set —
// the exact activation contract postgres_test.go already establishes — so CI
// proves the same guarantees against SKIP LOCKED, JSONB and DOUBLE PRECISION
// while a local run stays green without a database.

import (
	"context"
	"database/sql"
	"os"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
)

// storeFixture is the store-level surface a guarantee test needs: the Store
// itself plus a way to read rows back directly, so the assertions see what
// the engine wrote rather than what the store reports.
type storeFixture struct {
	Store
	db *sql.DB
}

// newStoreFixture builds a SQLite fixture (always available).
func newStoreFixture(t *testing.T) *storeFixture {
	t.Helper()
	s := newTestStore(t)
	return &storeFixture{Store: s, db: storeDB(t, s)}
}

// newPGFixture builds a PostgreSQL fixture when the DSN is configured, and
// skips otherwise — the same activation contract as newPGSuite.
func newPGFixture(t *testing.T) *storeFixture {
	t.Helper()
	if os.Getenv(pgTestEnv) == "" {
		t.Skipf("%s not set; skipping real-PostgreSQL delivery-guarantee tests", pgTestEnv)
	}
	suite := newPGSuite(t)
	return &storeFixture{Store: suite.store, db: suite.db()}
}

// ph rewrites a `?`-parameterized statement into the placeholder style the
// fixture's backend speaks. Both engines run the SAME guarantee suite, so the
// assertions are written once in the SQLite dial and translated here rather
// than duplicated per backend: a divergence between the two engines' row
// shapes is exactly what these tests exist to catch.
func (f *storeFixture) ph(query string, args ...any) (string, []any) {
	if _, ok := f.Store.(*sqliteStore); ok {
		return query, args
	}
	out := make([]byte, 0, len(query))
	n := 0
	for i := 0; i < len(query); i++ {
		if query[i] == '?' {
			n++
			out = append(out, []byte("$"+itoaPG(n))...)
			continue
		}
		out = append(out, query[i])
	}
	return string(out), args
}

// itoaPG avoids strconv in a test helper kept small on purpose.
func itoaPG(n int) string {
	if n == 0 {
		return "0"
	}
	digits := ""
	for n > 0 {
		digits = string(rune('0'+n%10)) + digits
		n /= 10
	}
	return digits
}

// queryRow runs a backend-neutral SELECT.
func (f *storeFixture) queryRow(t *testing.T, query string, args ...any) *sql.Row {
	t.Helper()
	q, a := f.ph(query, args...)
	return f.db.QueryRow(q, a...)
}

// exec runs a backend-neutral statement and reports whether a row changed,
// so a seed can fail loudly on the shape it cares about (a duplicate seed the
// UNIQUE key refused, for example) instead of silently passing through.
func (f *storeFixture) exec(t *testing.T, query string, args ...any) (sql.Result, error) {
	t.Helper()
	q, a := f.ph(query, args...)
	return f.db.Exec(q, a...)
}

// jobStatus reads one delivery_jobs.status, failing the test on a missing row.
func (f *storeFixture) jobStatus(t *testing.T, jobID string) string {
	t.Helper()
	var status string
	if err := f.queryRow(t, `SELECT status FROM delivery_jobs WHERE id=?`, jobID).Scan(&status); err != nil {
		t.Fatalf("read status for %s: %v", jobID, err)
	}
	return status
}

// jobAttempts reads the spent attempt count.
func (f *storeFixture) jobAttempts(t *testing.T, jobID string) int {
	t.Helper()
	var attempts int
	if err := f.queryRow(t, `SELECT attempts FROM delivery_jobs WHERE id=?`, jobID).Scan(&attempts); err != nil {
		t.Fatalf("read attempts for %s: %v", jobID, err)
	}
	return attempts
}

// fenceStatus reads one delivery_attempts.status, "" when the row is absent.
func (f *storeFixture) fenceStatus(t *testing.T, key job.AttemptKey) string {
	t.Helper()
	var status string
	if err := f.queryRow(t, `SELECT status FROM delivery_attempts WHERE id=?`, key.String()).Scan(&status); err != nil {
		if err == sql.ErrNoRows {
			return ""
		}
		t.Fatalf("read fence %s: %v", key.String(), err)
	}
	return status
}

// ---------------------------------------------------------------------------
// 1. Idempotency: duplicate Enqueue is a no-op, never a second delivery.
// ---------------------------------------------------------------------------

// runEnqueueDuplicateIsNoop is the guarantee, expressed once against any
// backend: the same logical job (same rule/source/message/target/kind)
// enqueued N times leaves ONE row, one claimable delivery and one fence key.
func runEnqueueDuplicateIsNoop(t *testing.T, f *storeFixture) {
	t.Helper()
	ctx := context.Background()
	j := &job.DeliveryJob{
		ID: "r1:-100123:42:-100456", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 42, TargetChatID: "-100456", MaxAttempts: 5,
		JobKind: "forward",
		Payload: *job.NewPayload(map[string]any{"text": "hello", "session_id": "s1"}),
	}

	first, err := f.Enqueue(ctx, j)
	if err != nil {
		t.Fatalf("first enqueue: %v", err)
	}
	if !first {
		t.Fatal("the first enqueue reported not inserted")
	}

	// The duplicates the guarantee covers: a replayed event, a repair path,
	// a crash between the send and the commit — all re-enqueue the SAME
	// logical job.
	for i := 0; i < 4; i++ {
		inserted, err := f.Enqueue(ctx, j)
		if err != nil {
			t.Fatalf("duplicate enqueue %d: %v", i, err)
		}
		if inserted {
			t.Fatalf("duplicate enqueue %d reported inserted — a second delivery was scheduled", i)
		}
	}

	// Exactly one row exists, so at most one delivery can ever happen.
	var rows int
	if err := f.queryRow(t,
		`SELECT COUNT(*) FROM delivery_jobs WHERE rule_id=? AND source_chat_id=? AND source_message_id=? AND target_chat_id=?`,
		j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID).Scan(&rows); err != nil {
		t.Fatalf("count jobs: %v", err)
	}
	if rows != 1 {
		t.Fatalf("rows = %d, want 1 (enqueue must be idempotent on the UNIQUE key)", rows)
	}

	// And that one row is claimable exactly once: one claim drains the lane.
	claimed, err := f.ClaimBatch(ctx, "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed %d jobs, want 1 (one enqueue = one delivery)", len(claimed))
	}
}

func TestEnqueueDuplicateIsNoop_SQLite(t *testing.T) {
	runEnqueueDuplicateIsNoop(t, newStoreFixture(t))
}

func TestEnqueueDuplicateIsNoop_Postgres(t *testing.T) {
	runEnqueueDuplicateIsNoop(t, newPGFixture(t))
}

// ---------------------------------------------------------------------------
// 2. Ambiguous outcome: MarkUnknown, never retried, never SENT.
// ---------------------------------------------------------------------------

// runAmbiguousOutcomeIsUnknownNeverSent is the guarantee: after MarkUnknown
// the job is UNKNOWN (claimable by NOTHING — not a retry, not a sentinel
// that would re-dispatch it) and the fence row agrees.
func runAmbiguousOutcomeIsUnknownNeverSent(t *testing.T, f *storeFixture) {
	t.Helper()
	ctx := context.Background()
	j := &job.DeliveryJob{
		ID: "r1:-100123:43:-100456", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 43, TargetChatID: "-100456", Status: job.StatusClaimed,
		MaxAttempts: 5, JobKind: "forward",
	}
	if _, err := f.exec(t, `INSERT INTO delivery_jobs
		(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json,
		 status, attempts, max_attempts, next_retry_at, lease_until, worker_id,
		 error_detail, created_at, updated_at, intermediate_chat_id,
		 intermediate_message_id, delivery_stage, job_kind)
		VALUES (?,?,?,?,?,'{}',?,?,?,?,?, 'w1', '', 0, 0, '', NULL, 'DIRECT', 'forward')`,
		j.ID, j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID,
		string(j.Status), j.Attempts, j.MaxAttempts, j.NextRetryAt, j.LeaseUntil); err != nil {
		t.Fatalf("seed job: %v", err)
	}

	// The ambiguous outcome: Telegram's reply never came back.
	if err := f.MarkUnknown(ctx, j.ID, "Uncertain outcome: rpc timeout"); err != nil {
		t.Fatalf("mark unknown: %v", err)
	}

	if status := f.jobStatus(t, j.ID); status != "UNKNOWN" {
		t.Fatalf("status = %s, want UNKNOWN (never SENT, never a blind retry)", status)
	}

	// A claim sweep must not resurrect it: UNKNOWN is terminal for the claim
	// predicate (PENDING/RETRY_WAIT/CLAIMED only), so no recovery loop can
	// re-dispatch a possibly-already-sent message.
	claimed, err := f.ClaimBatch(ctx, "w-recovery", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim after UNKNOWN: %v", err)
	}
	for _, c := range claimed {
		if c.ID == j.ID {
			t.Fatalf("an UNKNOWN job was re-claimed by the recovery loop — a duplicate send is now possible")
		}
	}

	// And the fence holds the same story for the other engine: the send was
	// claimed, the outcome was never confirmed, and no later claim may turn
	// that into a dispatch.
	key := job.AttemptKey{RuleID: j.RuleID, SourceChatID: j.SourceChatID,
		SourceMessageID: j.SourceMessageID, TargetChatID: j.TargetChatID, JobKind: "forward"}
	if _, _, err := f.FenceClaim(ctx, key, "w1", float64(time.Now().Add(time.Minute).Unix())); err != nil {
		t.Fatalf("seed the fence claim the dispatch held: %v", err)
	}
	if err := f.FenceRecordOutcome(ctx, key, "w1", "UNKNOWN", "rpc timeout"); err != nil {
		t.Fatalf("fence record outcome: %v", err)
	}
	if status := f.fenceStatus(t, key); status != "UNKNOWN" {
		t.Fatalf("fence = %s, want UNKNOWN (reconciliation owns this row)", status)
	}
	if out, _, err := f.FenceClaim(ctx, key, "w-other", float64(time.Now().Add(time.Minute).Unix())); err != nil {
		t.Fatalf("fence claim: %v", err)
	} else if out != job.FenceUnknown {
		t.Fatalf("post-UNKNOWN fence outcome = %s, want unknown (never blind redispatch)", out)
	}
}

func TestAmbiguousOutcomeIsUnknownNeverSent_SQLite(t *testing.T) {
	runAmbiguousOutcomeIsUnknownNeverSent(t, newStoreFixture(t))
}

func TestAmbiguousOutcomeIsUnknownNeverSent_Postgres(t *testing.T) {
	runAmbiguousOutcomeIsUnknownNeverSent(t, newPGFixture(t))
}

// ---------------------------------------------------------------------------
// 3. Worker crash: lease expiry without completion returns the job to the
//    queue — recovery without a double-send.
// ---------------------------------------------------------------------------

// runCrashedWorkerLeaseExpiryRecoversWithoutDoubleSend is the guarantee: the
// job comes back to the claimable set once its lease passes, AND the send-side
// fence (whose lease also passed) refuses to hand out a second dispatch.
func runCrashedWorkerLeaseExpiryRecoversWithoutDoubleSend(t *testing.T, f *storeFixture) {
	t.Helper()
	ctx := context.Background()
	j := &job.DeliveryJob{
		ID: "r1:-100123:44:-100456", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 44, TargetChatID: "-100456", Status: job.StatusClaimed,
		MaxAttempts: 5, JobKind: "forward", WorkerID: "dead-worker",
	}
	// A claim that already expired: the worker died mid-dispatch.
	j.LeaseUntil = float64(time.Now().Add(-time.Minute).Unix())
	if _, err := f.exec(t, `INSERT INTO delivery_jobs
		(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json,
		 status, attempts, max_attempts, next_retry_at, lease_until, worker_id,
		 error_detail, created_at, updated_at, intermediate_chat_id,
		 intermediate_message_id, delivery_stage, job_kind)
		VALUES (?,?,?,?,?,'{}',?,?,?,?,?,?, '', 0, 0, '', NULL, 'DIRECT', 'forward')`,
		j.ID, j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID,
		string(j.Status), j.Attempts, j.MaxAttempts, j.NextRetryAt, j.LeaseUntil, j.WorkerID); err != nil {
		t.Fatalf("seed job: %v", err)
	}

	// The dead worker also held the send fence, and it may have landed in the
	// target before dying — so the fence must never hand the send out again
	// by default.
	key := job.AttemptKey{RuleID: j.RuleID, SourceChatID: j.SourceChatID,
		SourceMessageID: j.SourceMessageID, TargetChatID: j.TargetChatID, JobKind: "forward"}
	if _, _, err := f.FenceClaim(ctx, key, "dead-worker", float64(time.Now().Add(-time.Minute).Unix())); err != nil {
		t.Fatalf("seed fence claim: %v", err)
	}
	if n, err := f.FenceExpireStale(ctx, float64(time.Now().Unix())); err != nil {
		t.Fatalf("expire stale: %v", err)
	} else if n != 1 {
		t.Fatalf("expired %d fence rows, want 1", n)
	}

	// 1. RECOVERY: the job returns to the queue.
	claimed, err := f.ClaimBatch(ctx, "w-recovery", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("recovery claim: %v", err)
	}
	if len(claimed) != 1 || claimed[0].ID != j.ID {
		t.Fatalf("an expired-lease job was not recovered: claimed %v", claimed)
	}
	if claimed[0].WorkerID != "w-recovery" {
		t.Fatalf("recovery left worker_id = %q, want w-recovery", claimed[0].WorkerID)
	}
	// The crash spent NO attempt: the dispatch never completed.
	if claimed[0].Attempts != 0 {
		t.Errorf("recovery claimed a job with attempts = %d, want 0 (the crash spent none)", claimed[0].Attempts)
	}

	// 2. NO DOUBLE-SEND: the surviving worker claims the job back, but the
	// fence resolves to UNKNOWN (the send may have landed) — never dispatch.
	out, _, err := f.FenceClaim(ctx, key, "w-recovery", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence claim on the recovered job: %v", err)
	}
	if out != job.FenceUnknown {
		t.Fatalf("recovered job fence outcome = %s, want unknown (an expired claim never re-dispatches by default)", out)
	}
}

func TestCrashedWorkerLeaseExpiryRecoversWithoutDoubleSend_SQLite(t *testing.T) {
	runCrashedWorkerLeaseExpiryRecoversWithoutDoubleSend(t, newStoreFixture(t))
}

func TestCrashedWorkerLeaseExpiryRecoversWithoutDoubleSend_Postgres(t *testing.T) {
	runCrashedWorkerLeaseExpiryRecoversWithoutDoubleSend(t, newPGFixture(t))
}

// ---------------------------------------------------------------------------
// 4. Partial multi-target failure: one target's failure never touches the
//    other target's recorded success.
// ---------------------------------------------------------------------------

// runPartialMultiTargetFailureIsolatesTargets is the guarantee: the queue is
// per-target, so a job that failed for one target coexists with a job that
// succeeded for another — the failure never rolls the success back, and the
// failed job keeps its own attempt budget and dead-letter path.
func runPartialMultiTargetFailureIsolatesTargets(t *testing.T, f *storeFixture) {
	t.Helper()
	ctx := context.Background()

	// One event, two targets.
	for _, target := range []string{"-100A", "-100B"} {
		j := &job.DeliveryJob{
			ID: "r1:-100123:45:" + target, RuleID: "r1", SourceChatID: "-100123",
			SourceMessageID: 45, TargetChatID: target, Status: job.StatusPending,
			MaxAttempts: 2, JobKind: "forward",
		}
		if _, err := f.exec(t, `INSERT INTO delivery_jobs
			(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json,
			 status, attempts, max_attempts, next_retry_at, lease_until, worker_id,
			 error_detail, created_at, updated_at, intermediate_chat_id,
			 intermediate_message_id, delivery_stage, job_kind)
			VALUES (?,?,?,?,?,'{}',?,?,?,?,?, '', '', 0, 0, '', NULL, 'DIRECT', 'forward')`,
			j.ID, j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID,
			string(j.Status), j.Attempts, j.MaxAttempts, j.NextRetryAt, j.LeaseUntil); err != nil {
			t.Fatalf("seed job for %s: %v", target, err)
		}
	}

	// Target A succeeds: confirmed send, mapping recorded.
	if err := f.MarkSent(ctx, "r1:-100123:45:-100A", 9001); err != nil {
		t.Fatalf("mark sent A: %v", err)
	}
	// Target B fails permanently.
	if err := f.MarkFailed(ctx, "r1:-100123:45:-100B", "PERMISSION_DENIED: bot was blocked"); err != nil {
		t.Fatalf("mark failed B: %v", err)
	}

	// The two outcomes are independent rows: one SENT, one FAILED.
	if status := f.jobStatus(t, "r1:-100123:45:-100A"); status != "SENT" {
		t.Fatalf("target A status = %s, want SENT (the other target's failure must not touch it)", status)
	}
	if status := f.jobStatus(t, "r1:-100123:45:-100B"); status != "FAILED" {
		t.Fatalf("target B status = %s, want FAILED", status)
	}

	// The successful delivery is not pending work: the backlog counts
	// neither the SENT nor the FAILED job.
	if pending, err := f.PendingCount(ctx); err != nil {
		t.Fatalf("pending count: %v", err)
	} else if pending != 0 {
		t.Errorf("pending = %d, want 0 (both targets are resolved)", pending)
	}

	// And a later retry-shaped job for target B is still claimable: the
	// failed lane is not poisoned.
	retry := &job.DeliveryJob{
		ID: "r1:-100123:46:-100B", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 46, TargetChatID: "-100B", Status: job.StatusPending,
		MaxAttempts: 2, JobKind: "forward",
	}
	if _, err := f.exec(t, `INSERT INTO delivery_jobs
		(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json,
		 status, attempts, max_attempts, next_retry_at, lease_until, worker_id,
		 error_detail, created_at, updated_at, intermediate_chat_id,
		 intermediate_message_id, delivery_stage, job_kind)
		VALUES (?,?,?,?,?,'{}',?,?,?,?,?, '', '', 0, 0, '', NULL, 'DIRECT', 'forward')`,
		retry.ID, retry.RuleID, retry.SourceChatID, retry.SourceMessageID, retry.TargetChatID,
		string(retry.Status), retry.Attempts, retry.MaxAttempts, retry.NextRetryAt, retry.LeaseUntil); err != nil {
		t.Fatalf("seed retry job: %v", err)
	}
	claimed, err := f.ClaimBatch(ctx, "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim after partial failure: %v", err)
	}
	if len(claimed) != 1 || claimed[0].ID != retry.ID {
		t.Fatalf("claim = %v, want exactly the target-B retry job (the failed target's lane stays open)", claimed)
	}
}

func TestPartialMultiTargetFailureIsolatesTargets_SQLite(t *testing.T) {
	runPartialMultiTargetFailureIsolatesTargets(t, newStoreFixture(t))
}

func TestPartialMultiTargetFailureIsolatesTargets_Postgres(t *testing.T) {
	runPartialMultiTargetFailureIsolatesTargets(t, newPGFixture(t))
}

// ---------------------------------------------------------------------------
// 5. Album delivery integrity: checkpointing merges, and a terminal row is
//    never rewritten.
// ---------------------------------------------------------------------------

// runAlbumProgressCheckpointsMerge is the guarantee: per-item confirmations
// accumulate (a later write never drops an earlier one) and a SENT job is
// never rewritten — the audit E2E-5 condition (a retry duplicated every
// item Telegram had already accepted).
func runAlbumProgressCheckpointsMerge(t *testing.T, f *storeFixture) {
	t.Helper()
	ctx := context.Background()
	j := &job.DeliveryJob{
		ID: "r1:-100123:47:-100456", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 47, TargetChatID: "-100456", Status: job.StatusClaimed,
		MaxAttempts: 5, JobKind: "forward",
	}
	if _, err := f.exec(t, `INSERT INTO delivery_jobs
		(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json,
		 status, attempts, max_attempts, next_retry_at, lease_until, worker_id,
		 error_detail, created_at, updated_at, intermediate_chat_id,
		 intermediate_message_id, delivery_stage, job_kind)
		VALUES (?,?,?,?,?,'{}',?,?,?,?,?, '', '', 0, 0, '', NULL, 'DIRECT', 'forward')`,
		j.ID, j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID,
		string(j.Status), j.Attempts, j.MaxAttempts, j.NextRetryAt, j.LeaseUntil); err != nil {
		t.Fatalf("seed job: %v", err)
	}

	// Partial failure three items in: the first two are confirmed and
	// checkpointed, the third never landed.
	if err := f.RecordAlbumProgress(ctx, j.ID, map[string]int64{"101": 2001, "102": 2002}); err != nil {
		t.Fatalf("record progress 1: %v", err)
	}
	// The retry confirms the remaining item and re-asserts the earlier ones
	// (a resume writes the whole known-sent map, not a delta).
	if err := f.RecordAlbumProgress(ctx, j.ID, map[string]int64{"101": 2001, "102": 2002, "103": 2003}); err != nil {
		t.Fatalf("record progress 2: %v", err)
	}

	var payloadJSON string
	if err := f.queryRow(t, `SELECT payload_json FROM delivery_jobs WHERE id=?`, j.ID).Scan(&payloadJSON); err != nil {
		t.Fatalf("read payload: %v", err)
	}
	for _, want := range []string{`"101"`, `"102"`, `"103"`} {
		if !contains(payloadJSON, want) {
			t.Errorf("album checkpoint lost item %s: payload = %s", want, payloadJSON)
		}
	}
	// Postgres renders JSONB with a space after the colon ("<k>: <v>")
	// while SQLite does not, so the assertion reads the numeric target id
	// itself: the point is that the confirmed ids survived the merge, not
	// which spacing either driver chose.
	for _, want := range []string{"2001", "2002", "2003"} {
		if !contains(payloadJSON, want) {
			t.Errorf("album checkpoint lost the target id %s: payload = %s", want, payloadJSON)
		}
	}

	// A terminal (SENT) row is never rewritten by a late progress write:
	// the delivery is over, and mutating its payload would rewrite history
	// a reconciliation could be reading at the same moment.
	if err := f.MarkSent(ctx, j.ID, 2001); err != nil {
		t.Fatalf("mark sent: %v", err)
	}
	if err := f.RecordAlbumProgress(ctx, j.ID, map[string]int64{"999": 9999}); err != nil {
		t.Fatalf("record progress on a SENT job: %v", err)
	}
	var after string
	if err := f.queryRow(t, `SELECT payload_json FROM delivery_jobs WHERE id=?`, j.ID).Scan(&after); err != nil {
		t.Fatalf("read payload after: %v", err)
	}
	if contains(after, "999") {
		t.Errorf("a late album-progress write mutated a SENT job's payload: %s", after)
	}
}

func TestAlbumProgressCheckpointsMerge_SQLite(t *testing.T) {
	runAlbumProgressCheckpointsMerge(t, newStoreFixture(t))
}

func TestAlbumProgressCheckpointsMerge_Postgres(t *testing.T) {
	runAlbumProgressCheckpointsMerge(t, newPGFixture(t))
}

// ---------------------------------------------------------------------------
// 6. Edit sync: the fence collapses an edit onto the already-confirmed send.
// ---------------------------------------------------------------------------

// runEditSyncCollapsesOntoConfirmedSend is the guarantee: the fence is keyed
// on the ORIGINAL event (rule/source/message/target), so the job an edit
// produces resolves to already_sent — no duplicate send, and the cadence
// slot the original consumed is not spent a second time.
func runEditSyncCollapsesOntoConfirmedSend(t *testing.T, f *storeFixture) {
	t.Helper()
	ctx := context.Background()

	// The original delivery, confirmed.
	key := job.AttemptKey{RuleID: "r1", SourceChatID: "-100123", SourceMessageID: 50,
		TargetChatID: "-100456", JobKind: "forward"}
	if _, _, err := f.FenceClaim(ctx, key, "w1", float64(time.Now().Add(time.Minute).Unix())); err != nil {
		t.Fatalf("original fence claim: %v", err)
	}
	if err := f.FenceRecordSent(ctx, key, "w1", 5001); err != nil {
		t.Fatalf("record the confirmed send: %v", err)
	}

	// The edit arrives as ANOTHER job for the same event — Python's
	// fold_edit keeps the job's identity fields on the original message, so
	// the fence key is identical.
	editJob := &job.DeliveryJob{
		ID: "r1:-100123:50:-100456", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 50, TargetChatID: "-100456", Status: job.StatusPending,
		MaxAttempts: 5, JobKind: "forward",
		Payload: *job.NewPayload(map[string]any{
			"text": "edited body", "session_id": "s1",
			"is_edit_folded": true, "edit_date": float64(time.Now().Unix()),
		}),
	}
	if inserted, err := f.Enqueue(ctx, editJob); err != nil {
		t.Fatalf("enqueue the edit job: %v", err)
	} else if !inserted {
		// The edit folded into the SAME durable job row (fold_edit's own
		// path): the guarantee then is that the job's fence still says the
		// send is confirmed, which the claim below proves.
		_ = inserted
	}

	out, row, err := f.FenceClaim(ctx, key, "w-edit", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("edit fence claim: %v", err)
	}
	if out != job.FenceAlreadySent {
		t.Fatalf("edit fence outcome = %s, want already_sent (the edit must collapse onto the confirmed send)", out)
	}
	if row == nil || row.TargetMessageID != 5001 {
		t.Fatalf("edit fence row = %+v, want the original's target id 5001", row)
	}

	// Exactly one send exists for the event: the fence never granted a
	// second dispatch.
	var sentClaims int
	if err := f.queryRow(t,
		`SELECT COUNT(*) FROM delivery_attempts WHERE id=? AND status='SENT'`, key.String()).Scan(&sentClaims); err != nil {
		t.Fatalf("count confirmed sends: %v", err)
	}
	if sentClaims != 1 {
		t.Fatalf("confirmed sends = %d, want exactly 1 (no duplicate send for the edit)", sentClaims)
	}
}

func TestEditSyncCollapsesOntoConfirmedSend_SQLite(t *testing.T) {
	runEditSyncCollapsesOntoConfirmedSend(t, newStoreFixture(t))
}

func TestEditSyncCollapsesOntoConfirmedSend_Postgres(t *testing.T) {
	runEditSyncCollapsesOntoConfirmedSend(t, newPGFixture(t))
}

// ---------------------------------------------------------------------------
// 7. Rate limit: Defer spends no attempt and releases the lane.
// ---------------------------------------------------------------------------

// runRateLimitDeferReleasesTheLane is the guarantee: a throttled target
// defers the job (attempts untouched), and once the window elapses the job
// is claimable again — the lane was never held, so a worker never sits idle
// behind a throttle it already honored.
func runRateLimitDeferReleasesTheLane(t *testing.T, f *storeFixture) {
	t.Helper()
	ctx := context.Background()
	j := &job.DeliveryJob{
		ID: "r1:-100123:51:-100456", RuleID: "r1", SourceChatID: "-100123",
		SourceMessageID: 51, TargetChatID: "-100456", Status: job.StatusPending,
		MaxAttempts: 5, JobKind: "forward",
	}
	if _, err := f.exec(t, `INSERT INTO delivery_jobs
		(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json,
		 status, attempts, max_attempts, next_retry_at, lease_until, worker_id,
		 error_detail, created_at, updated_at, intermediate_chat_id,
		 intermediate_message_id, delivery_stage, job_kind)
		VALUES (?,?,?,?,?,'{}',?,?,?,?,?, '', '', 0, 0, '', NULL, 'DIRECT', 'forward')`,
		j.ID, j.RuleID, j.SourceChatID, j.SourceMessageID, j.TargetChatID,
		string(j.Status), j.Attempts, j.MaxAttempts, j.NextRetryAt, j.LeaseUntil); err != nil {
		t.Fatalf("seed job: %v", err)
	}

	// The throttle: defer for the demanded window.
	if err := f.Defer(ctx, j.ID, "Target rate-limited", 30*time.Second); err != nil {
		t.Fatalf("defer: %v", err)
	}
	if attempts := f.jobAttempts(t, j.ID); attempts != 0 {
		t.Errorf("a rate-limit defer spent an attempt: attempts = %d, want 0", attempts)
	}
	if status := f.jobStatus(t, j.ID); status != "RETRY_WAIT" {
		t.Errorf("status = %s, want RETRY_WAIT (deferred, not failed)", status)
	}

	// While the window is in force the lane is gated on next_retry_at:
	// nothing is claimable, and nothing is in-flight either (no CLAIMED row
	// holds the target hostage).
	claimed, err := f.ClaimBatch(ctx, "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim inside the window: %v", err)
	}
	if len(claimed) != 0 {
		t.Fatalf("a deferred job was claimed before its window elapsed: %v", claimed)
	}

	// Once the window elapses the job is claimable again: the defer released
	// the lane, it did not abandon the job.
	// Release the window the way the clock eventually would: the job's
	// next_retry_at moved into the past, so the lane is open again.
	if _, err := f.exec(t, `UPDATE delivery_jobs SET next_retry_at=? WHERE id=?`,
		float64(time.Now().Add(-time.Second).Unix()), j.ID); err != nil {
		t.Fatalf("release the retry window: %v", err)
	}
	claimed, err = f.ClaimBatch(ctx, "w1", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("claim after the window: %v", err)
	}
	if len(claimed) != 1 || claimed[0].ID != j.ID {
		t.Fatalf("claim after the window = %v, want the deferred job (the lane was released)", claimed)
	}
	// Still no attempt spent: the whole throttle episode cost the job nothing.
	if claimed[0].Attempts != 0 {
		t.Errorf("attempts after the defer cycle = %d, want 0", claimed[0].Attempts)
	}
}

func TestRateLimitDeferReleasesTheLane_SQLite(t *testing.T) {
	runRateLimitDeferReleasesTheLane(t, newStoreFixture(t))
}

func TestRateLimitDeferReleasesTheLane_Postgres(t *testing.T) {
	runRateLimitDeferReleasesTheLane(t, newPGFixture(t))
}
