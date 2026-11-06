package delivery

import (
	"context"
	"strings"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
)

// Panic-containment contract: a panicking job must never take the worker
// process down. Before the containment existed, one poisoned payload
// crash-looped the worker forever — the restart re-claimed the same job and
// panicked again, stalling every delivery behind it.
//
// The containment hands the panicking job to the retry budget (MarkRetry:
// bounded attempts, dead-letter at exhaustion) and keeps the fence
// untouched — an outcome recorded before the panic (SENT) stays terminal,
// and an unrecorded one ages out to UNKNOWN exactly like any other
// ambiguity.

// panickingGateway panics on the first copy, then delegates: one poisoned
// job, a healthy worker afterwards.
type panickingGateway struct {
	*fakegateway.Fake
	panicOnSend bool
}

func (p *panickingGateway) SendMessage(ctx context.Context, sessionID string,
	targetChatID int64, text string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	if p.panicOnSend {
		p.panicOnSend = false
		panic("boom: poisoned payload")
	}
	return p.Fake.SendMessage(ctx, sessionID, targetChatID, text, threadID)
}

func (p *panickingGateway) CopyMessage(ctx context.Context, sessionID string,
	targetChatID, fromChatID, messageID int64, caption string, threadID int64,
	dropSourceButtons bool) (int64, *telegraph.DeliveryFailure) {
	if p.panicOnSend {
		p.panicOnSend = false
		panic("boom: poisoned payload")
	}
	return p.Fake.CopyMessage(ctx, sessionID, targetChatID, fromChatID,
		messageID, caption, threadID, dropSourceButtons)
}

func TestPanicInDeliveryIsContainedAndRetriedWithBudget(t *testing.T) {
	h := newHarness(t)
	gw := &panickingGateway{Fake: h.gateway, panicOnSend: true}
	h.engine.gateway = gw

	j := &job.DeliveryJob{
		ID:              "panic-job-1",
		RuleID:          "rule-1",
		SourceChatID:    "-100111",
		SourceMessageID: 1,
		TargetChatID:    "-100222",
		Status:          job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "hello"}),
	}
	h.enqueue(t, j)

	err := h.engine.Process(context.Background(), j)
	if err == nil {
		t.Fatal("a panic must surface as an error to the caller, not a silent success")
	}
	if !strings.Contains(err.Error(), "panic") {
		t.Fatalf("error should name the panic, got: %v", err)
	}

	// The worker is alive and its counters say what happened: the panic is
	// its own outcome class, never folded into a Telegram "failed".
	if h.metrics.unknown != 0 || h.metrics.failed != 0 {
		t.Fatalf("panic must not be counted as failed/unknown: %+v", h.metrics)
	}
	if h.metrics.panicked != 1 {
		t.Fatalf("panicked counter = %d, want 1", h.metrics.panicked)
	}

	// The job went back to the retry budget: RETRY_WAIT with one spent
	// attempt, re-claimable after the backoff — bounded, dead-lettered at
	// exhaustion, never a crash loop.
	assertJobStatus(t, h.store, j.ID, string(job.StatusRetryWait))
	db := storeDBOf(t, h.store)
	var attempts int
	var nextRetry float64
	if err := db.QueryRow(
		`SELECT attempts, next_retry_at FROM delivery_jobs WHERE id=?`, j.ID,
	).Scan(&attempts, &nextRetry); err != nil {
		t.Fatalf("read job after panic: %v", err)
	}
	if attempts != 1 {
		t.Fatalf("attempts after panic = %d, want 1 (the budget must bound a poison job)", attempts)
	}
	if nextRetry <= 0 {
		t.Fatal("a retried job must carry a backoff timestamp")
	}

	// The worker still delivers: a healthy job right after the poisoned one
	// goes through the same engine untouched.
	ok := &job.DeliveryJob{
		ID:              "panic-job-2",
		RuleID:          "rule-1",
		SourceChatID:    "-100111",
		SourceMessageID: 2,
		TargetChatID:    "-100222",
		Status:          job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "hello again"}),
	}
	h.enqueue(t, ok)
	if err := h.engine.Process(context.Background(), ok); err != nil {
		t.Fatalf("a healthy job after a panic must deliver normally: %v", err)
	}
	assertJobStatus(t, h.store, ok.ID, string(job.StatusSent))
}

func TestBackoffForIsBoundedExponential(t *testing.T) {
	cases := []struct {
		attempts int
		want     time.Duration
	}{
		{0, 1 * time.Second},
		{1, 2 * time.Second},
		{4, 16 * time.Second},
		{12, 5 * time.Minute}, // capped: no unbounded retry storm
		{99, 5 * time.Minute},
	}
	for _, c := range cases {
		if got := backoffFor(c.attempts); got != c.want {
			t.Errorf("backoffFor(%d) = %v, want %v", c.attempts, got, c.want)
		}
	}
}
