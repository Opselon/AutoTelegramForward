package delivery

import (
	"context"
	"database/sql"
	"strconv"
	"testing"

	"atf/worker/internal/domain/dedup"
	"atf/worker/internal/domain/job"
)

// --- Stage 4.5 (W3): event-level dedup preflight -------------------------- //
//
// The W3 block in Process closes a job whose event the task already
// processed as SENT-without-send. These tests pin the three properties that
// make that close safe rather than a silent drop:
//
//   - the close happens WITHOUT a Telegram call (nothing to re-send),
//   - the close happens WITHOUT claiming the send-side fence (no unresolved
//     CLAIMED row left behind),
//   - a store outage or an absent store forwards the message (the Python
//     ingest gate stays authoritative; this is defence-in-depth).

// dedupTestSchema is the Core's processed_messages + content_dedup DDL
// (core/infrastructure/persistence/database.py).
const dedupTestSchema = `
CREATE TABLE IF NOT EXISTS processed_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    media_group_id TEXT,
    ts INTEGER NOT NULL,
    UNIQUE(rule_id, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS content_dedup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(rule_id, content_hash)
);
`

// openDedupStore builds a dedup.Store over its own temp SQLite carrying the
// Core's dedup tables. Deliberately a SEPARATE database handle from the
// queue store: the production wiring hands the engine two stores over the
// same atf.db, and asserting against one shared file would hide a wiring
// that opened the wrong one.
func openDedupStore(t *testing.T) dedup.Store {
	t.Helper()
	dir := t.TempDir()
	path := dir + "/dedup.db"
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open dedup test db: %v", err)
	}
	if _, err := db.Exec(dedupTestSchema); err != nil {
		db.Close()
		t.Fatalf("apply dedup schema: %v", err)
	}
	db.Close()
	store, err := dedup.NewSQLite(context.Background(), path+"?_busy_timeout=10000&_journal_mode=WAL")
	if err != nil {
		t.Fatalf("new dedup store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// dedupHarness wires a delivery harness with the real dedup store attached,
// the way cmd/worker wires it in production.
func newDedupHarness(t *testing.T, opts ...Options) (*harness, dedup.Store) {
	t.Helper()
	h := newHarness(t, opts...)
	d := openDedupStore(t)
	h.engine.SetDedupStore(d)
	return h, d
}

// replayJob builds the kind of job the W3 gate exists for: the source event
// the task already accepted, sitting in the queue after Python marked it
// processed at enqueue time.
func replayJob() *job.DeliveryJob {
	return &job.DeliveryJob{
		ID: "r1:c:5:t", RuleID: "r1", SourceChatID: "c", SourceMessageID: 5,
		TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(map[string]any{"text": "hello world"}),
	}
}

// TestEventMarkedAtEnqueueStillDispatches is the regression test for the
// production defect: Python writes the processed_messages marker at ENQUEUE
// (pipeline.py:1444), so EVERY legitimately-enqueued first-time job carries
// it. A delivery-side gate that treats the marker as "already sent" drops
// 100% of new traffic — exactly the live failure on task_ea565d5c (jobs
// closed SENT with target_message_id=0, no Telegram call).
//
// The delivery-side idempotency is the send fence (FenceClaim /
// FenceAlreadySent), which collapses only onto a CONFIRMED send. The ingest
// marker belongs to the ingest gate that reads it (pipeline.py:867).
func TestEventMarkedAtEnqueueStillDispatches(t *testing.T) {
	h, d := newDedupHarness(t)
	ctx := context.Background()

	j := replayJob()
	if err := d.MarkProcessed(ctx, j.RuleID, j.SourceChatID, j.SourceMessageID, ""); err != nil {
		t.Fatalf("mark processed: %v", err)
	}
	h.enqueue(t, j)

	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Errorf("sent outcome = %d, want 1", h.metrics.sent)
	}
	if calls := h.gateway.Recorded(); len(calls) == 0 {
		t.Error("a job whose event was marked AT ENQUEUE never reached the gateway — the delivery engine is dropping new traffic")
	}
	if status := directStatus(t, h.store, j.ID); status != "SENT" {
		t.Errorf("job status = %s, want SENT", status)
	}
}

// TestEventNotProcessedDispatchesNormally: a fresh event must sail through
// the W3 gate and deliver for real. This is the guard against wiring the
// gate backwards (a gate that closes everything is just as broken as one
// that closes nothing).
func TestEventNotProcessedDispatchesNormally(t *testing.T) {
	h, _ := newDedupHarness(t)
	ctx := context.Background()

	j := replayJob()
	h.enqueue(t, j)

	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Errorf("sent outcome = %d, want 1", h.metrics.sent)
	}
	if calls := h.gateway.Recorded(); len(calls) == 0 {
		t.Error("a fresh event never reached the gateway — the W3 gate is closing events it should forward")
	}
}

// TestEventDedupIsScopedByRuleChatMessage pins the marker's key: the gate
// only closes an event the SAME task already saw. A different rule, source
// chat or message id is a different event and must deliver. The marker
// carries no target dimension by design (pipeline.py:1435).
func TestEventDedupIsScopedByRuleChatMessage(t *testing.T) {
	h, d := newDedupHarness(t)
	ctx := context.Background()

	if err := d.MarkProcessed(ctx, "r1", "c", 5, ""); err != nil {
		t.Fatalf("mark processed: %v", err)
	}
	for _, tc := range []struct {
		name                 string
		ruleID, chatID       string
		messageID            int64
		wantGatewayCallCount int
	}{
		{"same event still delivers (marker is written at enqueue)", "r1", "c", 5, 1},
		{"another task sees it fresh", "r2", "c", 5, 1},
		{"another source chat is fresh", "r1", "other", 5, 1},
		{"another message is fresh", "r1", "c", 6, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h.gateway.Reset()
			j := &job.DeliveryJob{
				ID:     tc.ruleID + ":" + tc.chatID + ":" + itoa(tc.messageID) + ":t",
				RuleID: tc.ruleID, SourceChatID: tc.chatID, SourceMessageID: tc.messageID,
				TargetChatID: "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
				Payload: *jobPayload(map[string]any{"text": "hello"}),
			}
			h.enqueue(t, j)
			if err := h.engine.Process(ctx, j); err != nil {
				t.Fatalf("process: %v", err)
			}
			if got := len(h.gateway.Recorded()); got != tc.wantGatewayCallCount {
				t.Errorf("gateway calls = %d, want %d", got, tc.wantGatewayCallCount)
			}
		})
	}
}

// TestEventDedupFailsOpenOnStoreError: a dedup store that errors must not
// become a reason to drop or defer the message. The Python ingest gate is
// authoritative; the Worker-side gate is defence-in-depth, and broken
// bookkeeping must not silence a task that was forwarding fine.
func TestEventDedupFailsOpenOnStoreError(t *testing.T) {
	h := newHarness(t)
	h.engine.SetDedupStore(errorDedupStore{})

	j := replayJob()
	h.enqueue(t, j)
	ctx := context.Background()
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if calls := h.gateway.Recorded(); len(calls) == 0 {
		t.Error("a dedup store outage dropped the message; the gate must fail open")
	}
	if status := directStatus(t, h.store, j.ID); status != "SENT" {
		t.Errorf("job status = %s, want SENT (delivered despite the store error)", status)
	}
}

// TestEventDedupAbsentStoreForwards: no store wired, pre-W3 behaviour —
// every existing construction site (and every existing test) keeps working.
func TestEventDedupAbsentStoreForwards(t *testing.T) {
	h := newHarness(t) // no SetDedupStore
	j := replayJob()
	h.enqueue(t, j)
	ctx := context.Background()
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if calls := h.gateway.Recorded(); len(calls) == 0 {
		t.Error("an unwired dedup store dropped the message")
	}
}

// TestEventDedupCloseLeavesNoFenceClaim pins the preflight ordering: the
// close happens BEFORE the send-side fence is claimed, so a SENT-without-send
// close cannot leave a CLAIMED fence row behind to expire into UNKNOWN —
// the exact failure mode the prelight-first ordering exists to prevent.
func TestEventDedupCloseLeavesNoFenceClaim(t *testing.T) {
	h, d := newDedupHarness(t)
	ctx := context.Background()

	j := replayJob()
	if err := d.MarkProcessed(ctx, j.RuleID, j.SourceChatID, j.SourceMessageID, ""); err != nil {
		t.Fatalf("mark processed: %v", err)
	}
	h.enqueue(t, j)
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("process: %v", err)
	}
	db := storeDBOf(t, h.store)
	var claimed int
	if err := db.QueryRow(`SELECT COUNT(*) FROM delivery_attempts WHERE status='CLAIMED'`).Scan(&claimed); err != nil {
		t.Fatalf("count claimed fences: %v", err)
	}
	if claimed != 0 {
		t.Errorf("a SENT-without-send close left %d CLAIMED fence row(s); the close must run before the fence is claimed", claimed)
	}
}

// TestEventDedupCloseIsIdempotentAcrossReclaims pins the FREEDOM from the
// old defect: with the marker written at enqueue (the production reality),
// a job re-claimed after a lease expiry must be collapsed by the FENCE onto
// its confirmed send — never by a "was it marked" guess that would also
// collapse first-time traffic.
func TestEventDedupCloseIsIdempotentAcrossReclaims(t *testing.T) {
	h, _ := newDedupHarness(t)
	ctx := context.Background()

	j := replayJob()
	h.enqueue(t, j)
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("first process: %v", err)
	}
	first := len(h.gateway.Recorded())
	if first == 0 {
		t.Fatal("first-time job never reached the gateway")
	}

	// A re-claim of the same job: the fence has a confirmed send, so no
	// duplicate Telegram call happens — that collapse is the fence's job.
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("replay process: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != first {
		t.Errorf("gateway called %d time(s) across replays, want %d (the fence collapses the replay)", got, first)
	}
}

type errorDedupStore struct{}

func (errorDedupStore) WasProcessed(context.Context, string, string, int64) (bool, error) {
	return false, errDedupUnavailable
}
func (errorDedupStore) MarkProcessed(context.Context, string, string, int64, string) error {
	return errDedupUnavailable
}
func (errorDedupStore) ClaimContentHash(context.Context, string, string) (bool, error) {
	return false, errDedupUnavailable
}
func (errorDedupStore) WasContentForwarded(context.Context, string, string) (bool, error) {
	return false, errDedupUnavailable
}
func (errorDedupStore) ReleaseContentHash(context.Context, string, string) error {
	return errDedupUnavailable
}
func (errorDedupStore) Close() error { return nil }

var errDedupUnavailable = &dedupError{"dedup store unavailable (test double)"}

type dedupError struct{ msg string }

func (e *dedupError) Error() string { return e.msg }

// itoa is the small int formatter these test ids need, kept local so the
// test file imports stay minimal.
func itoa(n int64) string { return strconv.FormatInt(n, 10) }
