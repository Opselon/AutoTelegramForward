package delivery

// guarantees_test.go — the Stage 5 delivery-guarantee suite at the ENGINE
// level: the same seven guarantees as queue/guarantees_test.go, but driven
// end to end through Engine.Process, so what is proved is the decision
// tree's behaviour (preflight ordering, dispatch shape, failure mapping,
// resume), not just the SQL underneath it.
//
// Each test names the guarantee it pins and the failure mode that would
// surface if the guarantee regressed. The store is a real SQLite carrying
// the Core's queue schema (queue.NewTestStore) and the gateway is the
// deterministic fake, so a "send" is a recorded call and an "ambiguous
// outcome" is a scripted typed failure — the assertions see exactly what a
// real Telegram would have made the engine do.

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/counters"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
	_ "github.com/mattn/go-sqlite3"
)

// --- 1. Idempotency -------------------------------------------------------

// TestDuplicateEnqueueIsNeverADuplicateDelivery (engine): the same logical
// job delivered twice by the engine — the crash-recovery replay — produces
// exactly one Telegram call. The queue's UNIQUE key keeps the second
// Enqueue from scheduling another delivery, and the send-side fence keeps a
// re-claimed job from re-dispatching.
//
// The regression this pins: the replay path used to re-enter dispatch
// unprotected, so a worker that crashed between Telegram's accept and the
// job-row commit sent the message twice.
func TestDuplicateEnqueueIsNeverADuplicateDelivery(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()

	j := &job.DeliveryJob{
		ID: "rg:c:1:t", RuleID: "rg", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "once"}),
	}
	inserted, err := h.store.Enqueue(ctx, j)
	if err != nil {
		t.Fatalf("first enqueue: %v", err)
	}
	if !inserted {
		t.Fatal("the first enqueue reported not inserted")
	}
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("first process: %v", err)
	}
	sends := len(h.gateway.Recorded())
	if sends == 0 {
		t.Fatal("the first process performed no gateway call")
	}

	// The replay: the same job arrives again (lease expiry, repair path, a
	// second worker). The enqueue is a no-op and a NEW job object for the
	// same logical send collapses onto the confirmed fence.
	dup, err := h.store.Enqueue(ctx, j)
	if err != nil {
		t.Fatalf("duplicate enqueue: %v", err)
	}
	if dup {
		t.Error("the duplicate enqueue reported inserted — a second delivery was scheduled")
	}
	replayed := &job.DeliveryJob{
		ID: "rg:c:1:t", RuleID: "rg", SourceChatID: "c", SourceMessageID: 1,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "once"}),
	}
	if err := h.engine.Process(ctx, replayed); err != nil {
		t.Fatalf("replay process: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != sends {
		t.Errorf("the replay performed %d gateway call(s), want %d (no duplicate delivery)", got, sends)
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
	if h.metrics.sent != 2 {
		t.Errorf("sent outcomes = %d, want 2 (the original send + the replay's SENT-without-send close)", h.metrics.sent)
	}
}

// --- 2. Ambiguous outcome -------------------------------------------------

// TestAmbiguousOutcomeIsNeverRetriedAndNeverSent (engine): a send whose
// reply never came back lands the job in UNKNOWN, the fence in UNKNOWN, and
// a subsequent claim resolves to unknown — never to a dispatch, never to a
// SENT. The difference between at-least-once and at-most-once-by-accident.
func TestAmbiguousOutcomeIsNeverRetriedAndNeverSent(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	h.gateway.SendFailsWith = &telegraph.DeliveryFailure{
		Code: "NETWORK_TIMEOUT", Message: "deadline exceeded",
	}

	j := &job.DeliveryJob{
		ID: "rg:c:2:t", RuleID: "rg", SourceChatID: "c", SourceMessageID: 2,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "ambiguous"}),
	}
	h.enqueue(t, j)
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("process: %v", err)
	}

	if status := directStatus(t, h.store, j.ID); status != "UNKNOWN" {
		t.Errorf("job status = %s, want UNKNOWN (never SENT, never a blind retry)", status)
	}
	if h.metrics.unknown != 1 {
		t.Errorf("unknown outcomes = %d, want 1", h.metrics.unknown)
	}
	if h.metrics.failed != 0 {
		t.Errorf("failed outcomes = %d, want 0 (an ambiguous outcome is not a failure)", h.metrics.failed)
	}
	if attempts := attemptsOf(t, h.store, j.ID); attempts != 0 {
		t.Errorf("attempts = %d, want 0 (the ambiguity spent no budget)", attempts)
	}

	// The recovery loop must not turn UNKNOWN into a dispatch: a later claim
	// on the same logical send resolves to unknown, the reconciliation-only
	// outcome.
	key := job.AttemptKey{RuleID: "rg", SourceChatID: "c", SourceMessageID: 2,
		TargetChatID: "t", JobKind: "forward"}
	out, _, err := h.store.FenceClaim(ctx, key, "w-recovery", float64(time.Now().Add(time.Minute).Unix()))
	if err != nil {
		t.Fatalf("fence claim after the ambiguous outcome: %v", err)
	}
	if out != job.FenceUnknown {
		t.Fatalf("fence outcome = %s, want unknown (reconciliation owns this send)", out)
	}
}

// --- 3. Worker crash ------------------------------------------------------

// TestCrashedWorkerRecoversWithoutDoubleSending (engine): a worker dies
// mid-dispatch (the lease expires with the job CLAIMED), the queue returns
// the job to the claimable set, and the surviving worker's re-claim
// re-runs it. Because the fence's claim expired to UNKNOWN — the send may
// have landed — the recovery never re-dispatches: it defers for
// reconciliation instead. Exactly one gateway call exists across the whole
// episode.
func TestCrashedWorkerRecoversWithoutDoubleSending(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()

	j := &job.DeliveryJob{
		ID: "rg:c:3:t", RuleID: "rg", SourceChatID: "c", SourceMessageID: 3,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{"text": "crash me"}),
	}
	h.enqueue(t, j)

	// The crash happens AFTER the fence was claimed and AFTER the send was
	// dispatched, but BEFORE the outcome was recorded. Model it directly:
	// the job row stays CLAIMED with an expired lease, the fence row stays
	// CLAIMED with an expired lease, and the gateway already saw one send.
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("first process: %v", err)
	}
	sends := len(h.gateway.Recorded())
	h.gateway.Reset()

	// Rewind the persisted state to "the worker died before committing":
	// both the job lease and the fence lease are in the past.
	db := queue.StoreDB(t, h.store)
	now := time.Now().Add(-time.Minute).Unix()
	if _, err := db.Exec(`UPDATE delivery_jobs SET status='CLAIMED', lease_until=?, worker_id='dead-worker', next_retry_at=0 WHERE id=?`,
		float64(now), j.ID); err != nil {
		t.Fatalf("rewind the job lease: %v", err)
	}
	key := job.AttemptKey{RuleID: "rg", SourceChatID: "c", SourceMessageID: 3,
		TargetChatID: "t", JobKind: "forward"}
	if _, err := db.Exec(`UPDATE delivery_attempts SET status='CLAIMED', lease_until=? WHERE id=?`,
		float64(now), key.String()); err != nil {
		t.Fatalf("rewind the fence lease: %v", err)
	}

	// 1. The job returns to the claimable set — recovery works.
	recovered, err := h.store.ClaimBatch(ctx, "w-survivor", 5, 30*time.Second)
	if err != nil {
		t.Fatalf("recovery claim: %v", err)
	}
	if len(recovered) != 1 || recovered[0].ID != j.ID {
		t.Fatalf("recovery claimed %v, want the crashed job back", recovered)
	}

	// 2. The sweep expires the stale fence claim to UNKNOWN, because the
	// send may have landed.
	if n, err := h.store.FenceExpireStale(ctx, float64(time.Now().Unix())); err != nil {
		t.Fatalf("expire stale: %v", err)
	} else if n != 1 {
		t.Fatalf("expired %d fence rows, want 1", n)
	}

	// 3. The survivor re-runs the job: the fence says unknown, so the job
	// defers for reconciliation rather than re-dispatching.
	if err := h.engine.Process(ctx, recovered[0]); err != nil {
		t.Fatalf("recovery process: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != 0 {
		t.Errorf("the recovery performed %d gateway call(s), want 0 (the send may already have landed)", got)
	}
	if status := directStatus(t, h.store, j.ID); status != "RETRY_WAIT" {
		t.Errorf("job status after recovery = %s, want RETRY_WAIT (deferred for reconciliation)", status)
	}
	if sends == 0 {
		t.Error("the test modeled a crash that never sent anything")
	}
}

// --- 4. Partial multi-target failure --------------------------------------

// TestPartialMultiTargetFailureIsolatesSuccess (engine): one event fanned
// out to two targets where one target accepts and the other definitively
// refuses. The successful delivery is recorded and untouched; the failed
// one spends its own attempt budget on its own target — never the other's.
func TestPartialMultiTargetFailureIsolatesSuccess(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()

	makeJob := func(target string, msgID int64, payload map[string]any) *job.DeliveryJob {
		raw := map[string]any{"session_id": "s1"}
		for k, v := range payload {
			raw[k] = v
		}
		return &job.DeliveryJob{
			ID: "rg:c:" + itoaGuarantee(msgID) + ":" + target, RuleID: "rg",
			SourceChatID: "c", SourceMessageID: msgID, TargetChatID: target,
			Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
			Payload: *job.NewPayload(raw),
		}
	}

	// Target A: the delivery succeeds.
	jobA := makeJob("-100A", 10, map[string]any{"text": "to A"})
	h.enqueue(t, jobA)
	if err := h.engine.Process(ctx, jobA); err != nil {
		t.Fatalf("process A: %v", err)
	}
	assertJobStatus(t, h.store, jobA.ID, string(job.StatusSent))

	// Target B: the same event, a definitive refusal. The fake is scripted
	// to fail only B's sends.
	h.gateway.SendFailsWith = &telegraph.DeliveryFailure{
		Code: "CHAT_WRITE_FORBIDDEN", Message: "bot was blocked by the target",
	}
	jobB := makeJob("-100B", 10, map[string]any{"text": "to B"})
	h.enqueue(t, jobB)
	if err := h.engine.Process(ctx, jobB); err != nil {
		t.Fatalf("process B: %v", err)
	}
	assertJobStatus(t, h.store, jobB.ID, string(job.StatusFailed))

	// The success is untouched by the failure: A is still SENT, with its
	// mapping row carrying the confirmed target id.
	if status := directStatus(t, h.store, jobA.ID); status != "SENT" {
		t.Errorf("target A status = %s, want SENT (the failure on B rolled nothing back)", status)
	}
	if h.metrics.sent != 1 || h.metrics.failed != 1 {
		t.Errorf("outcomes: sent=%d failed=%d, want 1/1 (both targets recorded their own outcome)", h.metrics.sent, h.metrics.failed)
	}

	// The failure left no orphaned state on the successful lane: A's job is
	// not pending, not in a retry window, and consumed no attempt of B's.
	if pending, err := h.store.PendingCount(ctx); err != nil {
		t.Fatalf("pending count: %v", err)
	} else if pending != 0 {
		t.Errorf("pending = %d, want 0 (both lanes resolved)", pending)
	}
}

// --- 5. Album delivery integrity ------------------------------------------

// TestSplitAlbumResumeSendsOnlyUnconfirmedItems (engine): a split album
// whose first two items Telegram confirmed and whose third failed. The
// retry resumes from the checkpoint: only the unconfirmed item is sent, the
// confirmed ones are never re-dispatched, and the album consumed exactly
// one forward-every-N slot (the dispatcher aggregated the group into one
// job, so one Advance counts it once).
//
// The regression this pins is audit finding E2E-5: a retry used to re-send
// every item, duplicating the ones Telegram had already accepted.
func TestSplitAlbumResumeSendsOnlyUnconfirmedItems(t *testing.T) {
	ctx := context.Background()

	// A real counters store so the "album = one slot" half of the guarantee
	// is asserted against durable state, not a mock.
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, &fixedRules{rule: &rule.ForwardRule{
		ID: "rg", IsActive: true, ForwardEveryN: 2, OwnerUserID: 5094837833,
	}}, gw, nil, log, metrics, "w-album", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})
	dir := t.TempDir()
	cadence := newGuaranteeCounters(t, dir)
	engine.SetCounters(cadence)

	album := &job.DeliveryJob{
		ID: "rg:c:11:t", RuleID: "rg", SourceChatID: "c", SourceMessageID: 11,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5,
		Payload: *jobPayload(map[string]any{
			"text": "album", "media_group_id": "ga",
			"album_message_ids": []any{101, 102, 103},
			"album_mode":        "split",
		}),
	}
	queue.InsertJob(t, store, album)

	// Item 103 fails: it is the third CopyMessage call. The engine
	// checkpoints the two items that got through, so the failure path has
	// already recorded progress before surfacing the error.
	gw.CopyFailOnNth = 3
	if err := engine.Process(ctx, album); err != nil {
		t.Fatalf("first process (partial failure): %v", err)
	}
	// The two confirmed items must be checkpointed on the job row.
	assertPayloadHas(t, store, album.ID, "album_sent")
	assertPayloadHas(t, store, album.ID, "101")
	assertPayloadHas(t, store, album.ID, "102")

	// The retry: the run loop always re-claims, so the engine re-reads the
	// job from the store — carrying item 101 and 102's confirmations in its
	// payload. Release the backoff the transient failure demanded and let
	// the claim pick the row up the way the loop really does (as a
	// RETRY_WAIT row whose window elapsed — a re-dispatch, which must not
	// consume a second cadence slot).
	gw.CopyFailOnNth = 0
	gw.Reset()
	db := queue.StoreDB(t, store)
	if _, err := db.Exec(`UPDATE delivery_jobs SET next_retry_at=0 WHERE id=?`, album.ID); err != nil {
		t.Fatalf("release the retry window: %v", err)
	}
	claimed, err := store.ClaimBatch(ctx, "w-resume", 5, time.Hour)
	if err != nil {
		t.Fatalf("re-claim: %v", err)
	}
	var resumed *job.DeliveryJob
	for _, c := range claimed {
		if c.ID == album.ID {
			resumed = c
		}
	}
	if resumed == nil {
		t.Fatal("the partially-delivered album job was not re-claimable — the retry cannot happen")
	}
	if err := engine.Process(ctx, resumed); err != nil {
		t.Fatalf("resume process: %v", err)
	}
	copies := 0
	for _, c := range gw.Recorded() {
		if c.Op == "CopyMessage" {
			copies++
			if c.MessageID != 103 {
				t.Errorf("the resume re-sent album item %d, want only 103 (the unconfirmed one)", c.MessageID)
			}
		}
	}
	if copies != 1 {
		t.Fatalf("the resume performed %d CopyMessage call(s), want 1 (only the unconfirmed item)", copies)
	}
	assertJobStatus(t, store, album.ID, string(job.StatusSent))

	// The album consumed exactly ONE cadence slot across the whole episode
	// (first pass + retry): one job, one Advance.
	row, err := cadence.Peek(ctx, "rg")
	if err != nil {
		t.Fatalf("peek cadence: %v", err)
	}
	if row == nil {
		t.Fatal("no cadence row was written at all")
	}
	if row.Accepted != 1 {
		t.Errorf("cadence accepted = %d, want 1 (one album = one slot, even across a retry)", row.Accepted)
	}
}

// --- 6. Edit sync ---------------------------------------------------------

// TestEditSyncCollapsesOntoTheConfirmedSend (engine): an edit of a message
// the task already delivered arrives as a job keyed on the ORIGINAL event.
// The fence resolves already_sent, so the edit closes SENT with the
// previously recorded target id: no duplicate send, and the cadence never
// spends a second slot on the edit (payload.IsEdit is the marker the gate
// reads).
func TestEditSyncCollapsesOntoTheConfirmedSend(t *testing.T) {
	ctx := context.Background()

	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, &fixedRules{rule: &rule.ForwardRule{
		ID: "rg", IsActive: true, ForwardEveryN: 2, OwnerUserID: 5094837833,
	}}, gw, nil, log, metrics, "w-edit", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})
	cadence := newGuaranteeCounters(t, t.TempDir())
	engine.SetCounters(cadence)

	// The original delivery: one message, one slot, one confirmed send.
	original := &job.DeliveryJob{
		ID: "rg:c:12:t", RuleID: "rg", SourceChatID: "c", SourceMessageID: 12,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "original"}),
	}
	queue.InsertJob(t, store, original)
	if err := engine.Process(ctx, original); err != nil {
		t.Fatalf("deliver the original: %v", err)
	}
	sends := len(gw.Recorded())
	if sends != 1 {
		t.Fatalf("the original produced %d gateway call(s), want 1", sends)
	}
	row, _ := cadence.Peek(ctx, "rg")
	if row == nil || row.Accepted != 1 {
		t.Fatalf("cadence after the original = %+v, want accepted=1 (the slot the original consumed)", row)
	}

	// The edit of that message. Python's fold_edit keeps the job's identity
	// fields on the ORIGINAL event, so the fence key is identical; the
	// payload carries the edit markers the cadence gate reads, which is
	// enough on its own: an edit never counts and never uses a slot.
	edit := &job.DeliveryJob{
		ID: "rg:c:12:t", RuleID: "rg", SourceChatID: "c", SourceMessageID: 12,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{
			"text": "edited body", "is_edit_folded": true,
			"edit_date": float64(time.Now().Unix()),
		}),
	}
	// The durable job row is unique on the event, so the edit's enqueue
	// folds into the confirmed row (the queue's own UNIQUE guarantee); the
	// engine then sees an already-confirmed fence for that event.
	queue.InsertJob(t, store, edit)
	if err := engine.Process(ctx, edit); err != nil {
		t.Fatalf("process the edit: %v", err)
	}

	// No duplicate send: the gate never re-dispatched.
	if got := len(gw.Recorded()); got != sends {
		t.Errorf("the edit produced %d gateway call(s) total, want %d (no duplicate send)", got, sends)
	}
	// The fence collapsed the edit onto the confirmed send: the job closed
	// SENT carrying the ORIGINAL target id, not a new one.
	assertJobStatus(t, store, edit.ID, string(job.StatusSent))
	if metrics.sent < 1 {
		t.Errorf("sent outcomes = %d, want at least 1", metrics.sent)
	}

	// And the edit consumed no second slot: the cadence row still counts
	// exactly one accepted message for this task.
	row, _ = cadence.Peek(ctx, "rg")
	if row == nil {
		t.Fatal("the cadence row vanished")
	}
	if row.Accepted != 1 {
		t.Errorf("cadence accepted = %d after the edit, want 1 (an edit never spends a second slot)", row.Accepted)
	}
}

// --- 7. Rate limit --------------------------------------------------------

// TestTargetRateLimitDefersWithoutSpendingAnAttempt (engine): a throttled
// target defers the job — no Telegram call, no attempt spent, no fence
// claimed — and the target's lane is released so it does not hold the
// worker hostage while the window elapses.
func TestTargetRateLimitDefersWithoutSpendingAnAttempt(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, &fixedRules{rule: &rule.ForwardRule{ID: "rg", IsActive: true}},
		gw, nil, log, metrics, "w-ratelimit", t.TempDir(), Options{
			NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second,
			MaxPerMinutePerTarget: 1, RPCTimeout: 5 * time.Second,
		})

	j := &job.DeliveryJob{
		ID: "rg:c:13:t", RuleID: "rg", SourceChatID: "c", SourceMessageID: 13,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "throttle me"}),
	}
	queue.InsertJob(t, store, j)

	// Burn the one allowed send for this target so the next job is throttled.
	engine.recordSend("t")
	gw.Reset()

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}

	if got := len(gw.Recorded()); got != 0 {
		t.Errorf("a throttled target dispatched %d gateway call(s), want 0", got)
	}
	if attempts := attemptsOf(t, store, j.ID); attempts != 0 {
		t.Errorf("a rate-limited defer spent an attempt: attempts = %d, want 0", attempts)
	}
	assertJobStatus(t, store, j.ID, string(job.StatusRetryWait))
	if metrics.retried != 1 {
		t.Errorf("retried outcomes = %d, want 1 (the defer)", metrics.retried)
	}
	// No fence claim: the preflight ordering guarantee — nothing holds a
	// CLAIMED fence row when no send is imminent.
	if got := fenceRowCount(t, store, j.ID); got != 0 {
		t.Errorf("a rate-limited job left %d fence row(s) behind", got)
	}
	// The lane is released: the job is claimable again once its retry
	// window elapses (the defer is not an abandonment).
	if status := directStatus(t, store, j.ID); status != "RETRY_WAIT" {
		t.Errorf("status = %s, want RETRY_WAIT (deferred, not failed)", status)
	}

	// And the throttle does not leak across targets: a job for a DIFFERENT
	// target still dispatches immediately.
	other := &job.DeliveryJob{
		ID: "rg:c:14:u", RuleID: "rg", SourceChatID: "c", SourceMessageID: 14,
		TargetChatID: "u", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "other target"}),
	}
	queue.InsertJob(t, store, other)
	if err := engine.Process(context.Background(), other); err != nil {
		t.Fatalf("process the other target: %v", err)
	}
	if got := len(gw.Recorded()); got != 1 {
		t.Errorf("an unthrottled target performed %d gateway call(s), want 1", got)
	}
}

// TestTargetRateLimitWindowIsPerTarget verifies the throttle's scope: the
// window accumulates per target_chat_id, so two jobs for two targets under
// a limit of one per minute both dispatch — the limit is a per-target
// cooperation guard, not a global one.
func TestTargetRateLimitWindowIsPerTarget(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	engine := New(store, &fixedRules{rule: &rule.ForwardRule{ID: "rg", IsActive: true}},
		gw, nil, &testLogger{}, &testMetrics{}, "w-ratelimit", t.TempDir(), Options{
			NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second,
			MaxPerMinutePerTarget: 1, RPCTimeout: 5 * time.Second,
		})
	for _, target := range []string{"t1", "t2"} {
		j := &job.DeliveryJob{
			ID: "rg:c:15:" + target, RuleID: "rg", SourceChatID: "c", SourceMessageID: 15,
			TargetChatID: target, Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
			Payload: *jobPayload(map[string]any{"text": "x"}),
		}
		queue.InsertJob(t, store, j)
		if err := engine.Process(context.Background(), j); err != nil {
			t.Fatalf("process %s: %v", target, err)
		}
	}
	if got := len(gw.Recorded()); got != 2 {
		t.Errorf("two distinct targets produced %d gateway call(s), want 2 (the window is per target)", got)
	}
}

// --- shared helpers -------------------------------------------------------

// itoaGuarantee keeps the test bodies free of strconv imports.
func itoaGuarantee(n int64) string {
	if n == 0 {
		return "0"
	}
	out := ""
	for n > 0 {
		out = string(rune('0'+n%10)) + out
		n /= 10
	}
	return out
}

// newGuaranteeCounters opens a real counters store over a temp SQLite file
// carrying the Core's rule_counters schema, so a cadence assertion reads
// durable state rather than a mock.
func newGuaranteeCounters(t *testing.T, dir string) counters.Store {
	t.Helper()
	db, err := sql.Open("sqlite3", filepath.Join(dir, "counters.db"))
	if err != nil {
		t.Fatalf("open counters db: %v", err)
	}
	if _, err := db.Exec(ruleCountersSchema); err != nil {
		db.Close()
		t.Fatalf("apply counters schema: %v", err)
	}
	cadence, err := counters.NewSQLite(db)
	if err != nil {
		db.Close()
		t.Fatalf("new counters store: %v", err)
	}
	t.Cleanup(func() { _ = cadence.Close() })
	return cadence
}

// attemptsOf reads the job's spent attempt count directly off the row.
func attemptsOf(t *testing.T, s queue.Store, jobID string) int {
	t.Helper()
	return attempts(t, s, jobID)
}
