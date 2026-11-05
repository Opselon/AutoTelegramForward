package delivery

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/counters"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph"
	"atf/worker/internal/telegraph/fakegateway"
)

// The W2 (Stage 4.5) delivery-gate tests: the forward-every-N cadence as
// the delivery engine applies it, over a REAL counters store on the REAL
// queue schema. Every case has a Python twin in
// core/tests/test_forward_every_n.py::TestThePipelineGate, and the parity
// harness compares the arithmetic both engines use.

// ruleCountersSchema is the Core's migration v27 DDL, the same shape
// counters.NewSQLite validates against in production.
const ruleCountersSchema = `
CREATE TABLE IF NOT EXISTS rule_counters (
    rule_id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL DEFAULT 0,
    every_n INTEGER NOT NULL DEFAULT 1,
    accepted INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);`

// fenRules serves one rule whose ForwardEveryN the test varies.
type fenRules struct {
	everyN int
}

func (r fenRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	return &rule.ForwardRule{
		ID:            ruleID,
		IsActive:      true,
		OwnerUserID:   5094837833,
		ForwardEveryN: r.everyN,
	}, nil, nil
}

// newFenHarness builds an engine wired to a real counters store over a
// real SQLite file, so the cadence survives across Process calls exactly
// as it must across a worker restart.
func newFenHarness(t *testing.T, everyN int, opts ...Options) (*harness, counters.Store) {
	t.Helper()
	store := queue.NewTestStore(t)
	gw := newFenGateway(t)
	metrics := &testMetrics{}
	log := &testLogger{}
	o := Options{NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second}
	if len(opts) > 0 {
		o = opts[0]
	}
	dir := t.TempDir()
	engine := New(store, fenRules{everyN: everyN}, gw, nil, log, metrics, "w-fen", dir, o)

	// A counters store over its own file, carrying the Core's schema.
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
	engine.SetCounters(cadence)
	return &harness{store: store, gateway: gw, engine: engine, metrics: metrics, log: log}, cadence
}

// newFenGateway is the same fake the other harnesses use; kept local so
// this file stays self-contained.
func newFenGateway(t *testing.T) *fakegateway.Fake {
	t.Helper()
	return fakegateway.New("")
}

// fenJob builds one PENDING forward job with an empty-but-valid payload.
func fenJob(messageID int64, extra map[string]any) *job.DeliveryJob {
	raw := map[string]any{"session_id": "s1"}
	for k, v := range extra {
		raw[k] = v
	}
	return &job.DeliveryJob{
		ID: fmt.Sprintf("r1:c:%d:t", messageID), RuleID: "r1", SourceChatID: "c",
		SourceMessageID: messageID,
		TargetChatID:    "t", Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *job.NewPayload(raw),
	}
}

// TestFenN1ForwardsEverythingAndWritesNoCounter pins the inert default:
// N = 1 forwards every message and the rule_counters row is never written,
// so an old task stays byte-for-byte alike (Python's
// test_n1_forward_everything_and_writes_no_counter).
func TestFenN1ForwardsEverythingAndWritesNoCounter(t *testing.T) {
	h, cadence := newFenHarness(t, 1)
	for messageID := int64(1); messageID <= 5; messageID++ {
		j := fenJob(messageID, map[string]any{"text": "hello"})
		h.enqueue(t, j)
		if err := h.engine.Process(context.Background(), j); err != nil {
			t.Fatalf("process %d: %v", messageID, err)
		}
	}
	if len(h.gateway.Recorded()) != 5 {
		t.Errorf("gateway calls = %d, want 5", len(h.gateway.Recorded()))
	}
	row, err := cadence.Peek(context.Background(), "r1")
	if err != nil {
		t.Fatalf("peek: %v", err)
	}
	if row != nil {
		t.Errorf("N=1 wrote a counter row: %+v (must be inert)", row)
	}
}

// TestFenEveryFifthMessageGoesThrough: N = 5 forwards messages 1, 6, 11 of
// the accepted ones and closes the rest as a cadence skip.
func TestFenEveryFifthMessageGoesThrough(t *testing.T) {
	h, cadence := newFenHarness(t, 5)
	for messageID := int64(1); messageID <= 12; messageID++ {
		j := fenJob(messageID, map[string]any{"text": "m"})
		h.enqueue(t, j)
		if err := h.engine.Process(context.Background(), j); err != nil {
			t.Fatalf("process %d: %v", messageID, err)
		}
	}
	if got := len(h.gateway.Recorded()); got != 3 {
		t.Errorf("gateway calls = %d, want 3 (messages 1, 6, 11)", got)
	}
	if h.metrics.rejected != 9 || h.metrics.failed != 0 {
		t.Errorf("outcomes = %d failed / %d rejected, want 9 rejected (cadence skips are deliberate non-deliveries, not failures)", h.metrics.failed, h.metrics.rejected)
	}
	row, err := cadence.Peek(context.Background(), "r1")
	if err != nil || row == nil {
		t.Fatalf("peek: %v (%v)", row, err)
	}
	if row.Accepted != 12 || row.EveryN != 5 {
		t.Errorf("counter = %+v, want accepted=12 every_n=5", row)
	}
}

// TestFenACadenceSkipIsNotAnAttempt: a skipped job is MarkFailed with the
// cadence reason, never a fence claim and never a retry attempt.
func TestFenACadenceSkipIsNotAnAttempt(t *testing.T) {
	h, _ := newFenHarness(t, 2)
	ctx := context.Background()
	// Seed the cadence so the next advance is sequence 2, which N=2 does not
	// own: message 1 already went, this job is the held one.
	if _, err := h.engine.CountersStore().Advance(ctx, "r1", 2, 1); err != nil {
		t.Fatalf("seed cadence: %v", err)
	}
	j := fenJob(2, map[string]any{"text": "held"})
	h.enqueue(t, j)
	if err := h.engine.Process(ctx, j); err != nil {
		t.Fatalf("process: %v", err)
	}
	// No gateway call happened and the outcome was a failure close.
	if calls := h.gateway.Recorded(); len(calls) != 0 {
		t.Errorf("gateway calls = %d, want 0 (a skip never reaches Telegram)", len(calls))
	}
	if h.metrics.sent != 0 || h.metrics.retried != 0 || h.metrics.failed != 0 || h.metrics.rejected != 1 {
		t.Errorf("outcomes: sent=%d retried=%d failed=%d rejected=%d, want rejected=1 only", h.metrics.sent, h.metrics.retried, h.metrics.failed, h.metrics.rejected)
	}
	if !strings.Contains(h.log.lines[0], "skipped by forward-every-n cadence") {
		t.Errorf("log lines = %v, want the cadence skip line first", h.log.lines)
	}
	// The job row is closed FAILED with the cadence reason, not left
	// PENDING (a retry) and not SENT.
	db := queue.StoreDB(t, h.store)
	var status, errDetail string
	if err := db.QueryRow("SELECT status, error_detail FROM delivery_jobs WHERE id=?", j.ID).Scan(&status, &errDetail); err != nil {
		t.Fatalf("read job row: %v", err)
	}
	if status != string(job.StatusFailed) {
		t.Errorf("job status = %s, want FAILED (a cadence skip is terminal)", status)
	}
	if !strings.Contains(errDetail, "cadence") {
		t.Errorf("error_detail = %q, want the cadence reason", errDetail)
	}
}

// TestFenCadenceSurvivesAcrossJobs: the counter is durable, so a fresh
// engine over the same file continues the sequence (Python's
// test_the_cadence_survives_a_warm_restart).
func TestFenRedispatchKeysOnClaimedFromSanity(t *testing.T) {
	// The claim, not the job object, is the gate's only source of the
	// first-dispatch / re-dispatch fact: a PENDING row claimed reads
	// ClaimedFrom=PENDING (a first dispatch, the counter advances), while a
	// RETRY_WAIT row released by backoff reads ClaimedFrom=RETRY_WAIT (a
	// re-dispatch, the counter must not). This is the invariant
	// TestFenARetryNeverConsumesASecondSlot depends on, pinned on its own so
	// a regression in the claim path fails here with the store's name on it.
	ctx := context.Background()
	store := queue.NewTestStore(t)

	first := fenJob(311, map[string]any{"text": "one"})
	if ins, err := store.Enqueue(ctx, first); err != nil || !ins {
		t.Fatalf("enqueue: ins=%v err=%v", ins, err)
	}
	pending, err := store.ClaimBatch(ctx, "w1", 5, time.Hour)
	if err != nil {
		t.Fatalf("claim a PENDING row: %v", err)
	}
	if len(pending) != 1 || pending[0].ClaimedFrom != job.StatusPending {
		t.Fatalf("a PENDING row claimed: ClaimedFrom=%q, want PENDING", firstOf(pending))
	}
	if err := store.MarkRetry(ctx, first.ID, "transient", 0); err != nil {
		t.Fatalf("mark the job RETRY_WAIT: %v", err)
	}
	retried, err := store.ClaimBatch(ctx, "w2", 5, time.Hour)
	if err != nil {
		t.Fatalf("claim a RETRY_WAIT row: %v", err)
	}
	if len(retried) != 1 || retried[0].ClaimedFrom != job.StatusRetryWait {
		t.Fatalf("a RETRY_WAIT row claimed: ClaimedFrom=%q, want RETRY_WAIT", firstOf(retried))
	}
}

func firstOf(js []*job.DeliveryJob) job.Status {
	if len(js) == 0 {
		return "<no rows claimed>"
	}
	return js[0].ClaimedFrom
}

func TestFenCadenceSurvivesAcrossJobs(t *testing.T) {
	h, cadence := newFenHarness(t, 3)
	ctx := context.Background()

	run := func(messageID int64) {
		t.Helper()
		j := fenJob(messageID, map[string]any{"text": "m"})
		h.enqueue(t, j)
		if err := h.engine.Process(ctx, j); err != nil {
			t.Fatalf("process %d: %v", messageID, err)
		}
	}
	run(1) // slot 1 -> goes
	run(2) // held
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Fatalf("gateway calls = %d, want 1 (message 1 only)", got)
	}

	// A fresh engine over the SAME counters file: the cadence continues
	// where it stopped (accepted=2), so message 3 is sequence 3 and held.
	h2, _ := newFenHarness(t, 3)
	_ = h2
	fresh := newFenHarnessSharedCounters(t, 3, cadence)
	j := fenJob(3, map[string]any{"text": "m"})
	fresh.enqueue(t, j)
	if err := fresh.engine.Process(ctx, j); err != nil {
		t.Fatalf("process 3: %v", err)
	}
	if got := len(fresh.gateway.Recorded()); got != 0 {
		t.Errorf("fresh engine: gateway calls = %d, want 0 (sequence 3 is not a slot)", got)
	}
	// Message 4 IS sequence 4 = slot.
	j = fenJob(4, map[string]any{"text": "m"})
	fresh.enqueue(t, j)
	if err := fresh.engine.Process(ctx, j); err != nil {
		t.Fatalf("process 4: %v", err)
	}
	if got := len(fresh.gateway.Recorded()); got != 1 {
		t.Errorf("fresh engine: gateway calls = %d after message 4, want 1 (slot)", got)
	}
	row, _ := cadence.Peek(ctx, "r1")
	if row == nil || row.Accepted != 4 {
		t.Errorf("counter = %+v, want accepted=4", row)
	}
}

// newFenHarnessSharedCounters wires a fresh engine over an EXISTING
// counters store, the restart case.
func newFenHarnessSharedCounters(t *testing.T, everyN int, cadence counters.Store) *harness {
	t.Helper()
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, fenRules{everyN: everyN}, gw, nil, log, metrics, "w-fen2", t.TempDir(),
		Options{NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second})
	engine.SetCounters(cadence)
	return &harness{store: store, gateway: gw, engine: engine, metrics: metrics, log: log}
}

// TestFenAnEditNeverCountsOrUsesASlot mirrors Python's
// test_an_edit_never_counts_and_never_uses_a_slot: an edit of an already-
// forwarded message forwards (the edit mirror) WITHOUT advancing the
// counter, so the next real message keeps its slot.
func TestFenAnEditNeverCountsOrUsesASlot(t *testing.T) {
	h, cadence := newFenHarness(t, 2)
	ctx := context.Background()

	first := fenJob(9, map[string]any{"text": "one"})
	h.enqueue(t, first)
	if err := h.engine.Process(ctx, first); err != nil {
		t.Fatalf("process first: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Fatalf("gateway calls = %d, want 1", got)
	}
	// The edit of the message just forwarded. The durable payload marks
	// edits the way fold_edit does (is_edit_folded + edit_date).
	edit := fenJob(9, map[string]any{"text": "one*", "is_edit_folded": true, "edit_date": 1761300000})
	h.enqueue(t, edit)
	if err := h.engine.Process(ctx, edit); err != nil {
		t.Fatalf("process edit: %v", err)
	}
	row, err := cadence.Peek(ctx, "r1")
	if err != nil {
		t.Fatalf("peek: %v", err)
	}
	if row != nil && row.Accepted != 1 {
		t.Errorf("counter accepted = %d, want 1 (an edit never consumes a slot)", row.Accepted)
	}

	// The edit closes SENT without re-dispatching: the fence's
	// AttemptKey is keyed on the ORIGINAL event (fold_edit keeps the
	// job's identity fields), so the already-confirmed send collapses
	// the edit into the same delivery — Python's "the queue's
	// idempotency key is what collapses them". One gateway call, not
	// two, is the correct answer here.
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Errorf("gateway calls = %d, want 1 (the edit collapses onto the confirmed send)", got)
	}
	if h.metrics.sent != 2 {
		t.Errorf("sent outcomes = %d, want 2 (the edit closes SENT without re-dispatching)", h.metrics.sent)
	}

	// The next real message is sequence 2, i.e. held — not sequence 3,
	// which is what counting the edit would have made it.
	next := fenJob(10, map[string]any{"text": "two"})
	h.enqueue(t, next)
	if err := h.engine.Process(ctx, next); err != nil {
		t.Fatalf("process next: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Errorf("gateway calls = %d, want 1 (message 10 is held at sequence 2)", got)
	}
	if h.metrics.rejected != 1 {
		t.Errorf("rejected outcomes = %d, want 1 (message 10 skipped by cadence)", h.metrics.rejected)
	}
}

// TestFenAnIsEditPayloadMarkerIsHonored: the ingest-side is_edit boolean
// (the other durable spelling) also keeps the counter untouched.
func TestFenAnIsEditPayloadMarkerIsHonored(t *testing.T) {
	h, cadence := newFenHarness(t, 3)
	ctx := context.Background()
	edit := fenJob(77, map[string]any{"text": "v2", "is_edit": true})
	h.enqueue(t, edit)
	if err := h.engine.Process(ctx, edit); err != nil {
		t.Fatalf("process edit: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Errorf("gateway calls = %d, want 1", got)
	}
	if row, _ := cadence.Peek(ctx, "r1"); row != nil {
		t.Errorf("counter row = %+v, want none (an edit never starts the cadence)", row)
	}
}

// TestFenAFilterDropNeverConsumesASlot: the gate runs after the filter
// gates, so a message the task rejects never enters the cadence. The
// caller gates on the filter outcome; Python's
// test_filtered_messages_never_consume_a_slot pins the same property.
func TestFenAFilterDropNeverConsumesASlot(t *testing.T) {
	// A filter that drops the message, checked BEFORE the cadence gate by
	// the preflight's ordering: the message must never reach the counter.
	// The caller gates on the filter outcome; Python's
	// test_filtered_messages_never_consume_a_slot pins the same property.
	engineRules := filteringFenRules{everyN: 3, drop: "do-not-send"}
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, engineRules, gw, nil, log, metrics, "w-fen3", t.TempDir(),
		Options{NumWorkers: 1, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second})
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "c.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := db.Exec(ruleCountersSchema); err != nil {
		t.Fatalf("schema: %v", err)
	}
	cadence2, err := counters.NewSQLite(db)
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	t.Cleanup(func() { _ = cadence2.Close() })
	engine.SetCounters(cadence2)

	j := fenJob(5, map[string]any{"text": "please do-not-send this"})
	queue.InsertJob(t, store, j)
	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if len(gw.Recorded()) != 0 || metrics.rejected != 1 || metrics.failed != 0 {
		t.Errorf("gateway=%d outcomes: failed=%d rejected=%d, want a filter drop (rejected=1, not a failure)", len(gw.Recorded()), metrics.failed, metrics.rejected)
	}
	if row, _ := cadence2.Peek(context.Background(), "r1"); row != nil {
		t.Errorf("counter row = %+v, want none (a filtered message never consumes a slot)", row)
	}
}

// filteringFenRules serves a rule with a persisted filter that drops the
// sentinel phrase, so the filter gate fires before the cadence gate.
type filteringFenRules struct {
	everyN int
	drop   string
}

func (r filteringFenRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	return &rule.ForwardRule{
			ID: ruleID, IsActive: true, OwnerUserID: 1, ForwardEveryN: r.everyN,
		}, &rule.FilterRule{
			ID:                "f1",
			IsEnabled:         true,
			BlacklistKeywords: []string{r.drop},
		}, nil
}

// TestFenBrokenCounterFailsOpen mirrors Python's
// test_a_broken_counter_forwards_instead_of_silencing_the_task: an Advance
// error forwards anyway and logs loudly, never silences the task.
func TestFenBrokenCounterFailsOpen(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, fenRules{everyN: 5}, gw, nil, log, metrics, "w-broken", t.TempDir(),
		Options{NumWorkers: 1, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second})
	engine.SetCounters(brokenCounters{})

	j := fenJob(1, map[string]any{"text": "hello"})
	queue.InsertJob(t, store, j)
	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if len(gw.Recorded()) != 1 {
		t.Errorf("gateway calls = %d, want 1 (a broken counter must forward)", len(gw.Recorded()))
	}
	found := false
	for _, line := range log.lines {
		if strings.Contains(line, "counter unavailable") {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("log lines = %v, want an ERROR about the counter being unavailable", log.lines)
	}
}

// brokenCounters is the failing-store double: every Advance errors, the
// way a full disk or a lost connection would.
type brokenCounters struct{}

func (brokenCounters) Advance(context.Context, string, int, int64) (int, error) {
	return 0, errBrokenCounter
}
func (brokenCounters) Peek(context.Context, string) (*counters.Row, error) {
	return nil, errBrokenCounter
}
func (brokenCounters) Reset(context.Context, string) (bool, error) {
	return false, errBrokenCounter
}
func (brokenCounters) Close() error { return nil }

var errBrokenCounter = errors.New("disk full")

// TestFenNilCountersForwardsEverything: an engine with no cadence store
// (an older embed, or the feature off) forwards every message — Python's
// test_task_without_the_port_forwards_everything.
func TestFenNilCountersForwardsEverything(t *testing.T) {
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, fenRules{everyN: 7}, gw, nil, log, metrics, "w-bare", t.TempDir(),
		Options{NumWorkers: 1, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second})
	// No SetCounters: the port is absent.
	for messageID := int64(1); messageID <= 7; messageID++ {
		j := fenJob(messageID, map[string]any{"text": "m"})
		queue.InsertJob(t, store, j)
		if err := engine.Process(context.Background(), j); err != nil {
			t.Fatalf("process %d: %v", messageID, err)
		}
	}
	if len(gw.Recorded()) != 7 {
		t.Errorf("gateway calls = %d, want 7 (no counters port means no cadence)", len(gw.Recorded()))
	}
}

// TestFenAnAlbumIsOneSlot: one job per media group consumes exactly one
// sequence number, regardless of the album's item count (Python's
// test_one_album_is_one_message_even_when_it_has_five_photos). The Go
// worker receives the aggregated job, so the job IS the album.
func TestFenAnAlbumIsOneSlot(t *testing.T) {
	h, cadence := newFenHarness(t, 2)
	ctx := context.Background()
	albumA := fenJob(101, map[string]any{"media_group_id": "ga", "album_message_ids": []any{101, 102, 103}})
	h.enqueue(t, albumA)
	if err := h.engine.Process(ctx, albumA); err != nil {
		t.Fatalf("process album A: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Fatalf("gateway calls = %d, want 1 (album A owns slot 1)", got)
	}
	albumB := fenJob(104, map[string]any{"media_group_id": "gb", "album_message_ids": []any{104, 105}})
	h.enqueue(t, albumB)
	if err := h.engine.Process(ctx, albumB); err != nil {
		t.Fatalf("process album B: %v", err)
	}
	row, _ := cadence.Peek(ctx, "r1")
	if row == nil || row.Accepted != 2 {
		t.Errorf("counter = %+v, want accepted=2 (two albums, two slots)", row)
	}
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Errorf("gateway calls = %d, want 1 (album B is held at slot 2)", got)
	}
}

// TestFenARetryNeverConsumesASecondSlot is the Stage 5 regression test for
// the gap the delivery-guarantee suite found: the Python gate runs once per
// message AT INGEST (pipeline.py:1065), and a delivery retry never re-enters
// it — the re-delivery is absorbed by the event-level dedupe instead
// (test_a_redelivered_message_does_not_advance_the_counter). The Go gate
// runs on every Process call, so without the re-dispatch exception a
// transient failure advanced the counter a second time for the same message,
// and under N=2 the retry itself then failed the job for "skipped by
// cadence" — a healthy delivery silently dead-lettered by its own recovery.
//
// The shape: N=2, the message owns slot 1 and delivers; a transient failure
// asks for a retry; the release re-claims the RETRY_WAIT row and the gate
// must pass it through WITHOUT advancing the counter again.
func TestFenARetryNeverConsumesASecondSlot(t *testing.T) {
	ctx := context.Background()

	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	log := &testLogger{}
	engine := New(store, fenRules{everyN: 2}, gw, nil, log, &testMetrics{}, "w-fen-retry",
		t.TempDir(), Options{NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second})
	dir := t.TempDir()
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
	engine.SetCounters(cadence)

	// Message 1 delivers and owns slot 1 (sequence 1 under N=2).
	first := fenJob(301, map[string]any{"text": "one"})
	if inserted, err := store.Enqueue(ctx, first); err != nil || !inserted {
		t.Fatalf("enqueue the first job: inserted=%v err=%v", inserted, err)
	}
	if err := engine.Process(ctx, first); err != nil {
		t.Fatalf("first process: %v", err)
	}
	if row, _ := cadence.Peek(ctx, "r1"); row == nil || row.Accepted != 1 {
		t.Fatalf("cadence after the first message = %+v, want accepted=1", row)
	}

	// Message 2 would be sequence 2 — held by the cadence. Its job is
	// closed as a skip, which is the queue's own RETRY_WAIT shape when the
	// send itself fails: MarkRetry, then the backoff release, then the
	// re-claim. The re-claimed row is what the run loop hands the engine,
	// and this is the pass that used to advance the counter a second time
	// for the same message — producing sequence 3, a slot the message never
	// owned, and a *later* message that did own it dead-lettered instead.
	second := fenJob(302, map[string]any{"text": "two"})
	if inserted, err := store.Enqueue(ctx, second); err != nil || !inserted {
		t.Fatalf("enqueue the second job: inserted=%v err=%v", inserted, err)
	}
	gw.Reset()
	gw.SendFailsWith = &telegraph.DeliveryFailure{Code: "TELEGRAM_TEMPORARY_ERROR", Message: "transient"}
	if err := store.MarkRetry(ctx, second.ID, "transient", 0); err != nil {
		t.Fatalf("mark the second job RETRY_WAIT: %v", err)
	}
	log.lines = nil
	reclaimed, err := store.ClaimBatch(ctx, "w-retry", 5, time.Hour)
	if err != nil {
		t.Fatalf("re-claim: %v", err)
	}
	if len(reclaimed) != 1 || reclaimed[0].ID != second.ID {
		t.Fatalf("claim = %v, want the same job back", reclaimed)
	}
	if reclaimed[0].ClaimedFrom != job.StatusRetryWait {
		t.Errorf("ClaimedFrom = %q, want RETRY_WAIT (the re-dispatch fact the gate keys on)", reclaimed[0].ClaimedFrom)
	}
	if err := engine.Process(ctx, reclaimed[0]); err != nil {
		t.Fatalf("re-dispatch process: %v", err)
	}
	if status := directStatus(t, store, second.ID); status != "RETRY_WAIT" {
		t.Fatalf("a transient failure left status %s, want RETRY_WAIT", status)
	}
	if n := attempts(t, store, second.ID); n != 2 {
		t.Fatalf("the two transient failures spent %d attempts, want 2 (one per send)", n)
	}
	if row, _ := cadence.Peek(ctx, "r1"); row == nil || row.Accepted != 1 {
		t.Fatalf("cadence after the re-dispatch = %+v, want accepted=1 (a retry advanced nothing)", row)
	}

	// The recovery: the backoff window opens and the row is re-claimed and
	// re-processed a second time — still the same message 302, still no
	// second slot. The send succeeds now, and the counter must still read 1.
	gw.SendFailsWith = nil
	if _, err := queue.StoreDB(t, store).Exec(
		`UPDATE delivery_jobs SET next_retry_at=0 WHERE id=?`, second.ID); err != nil {
		t.Fatalf("release the retry window: %v", err)
	}
	claimed, err := store.ClaimBatch(ctx, "w-retry-2", 5, time.Hour)
	if err != nil {
		t.Fatalf("re-claim after backoff: %v", err)
	}
	var retried *job.DeliveryJob
	for _, c := range claimed {
		if c.ID == second.ID {
			retried = c
		}
	}
	if retried == nil {
		t.Fatal("the retry row was not re-claimable — the recovery cannot happen")
	}
	if err := engine.Process(ctx, retried); err != nil {
		t.Fatalf("retry process: %v", err)
	}
	if status := directStatus(t, store, retried.ID); status != "SENT" {
		t.Fatalf("the retried job ended %s, want SENT (a retry must not be skipped by the cadence)", status)
	}
	if row, _ := cadence.Peek(ctx, "r1"); row == nil || row.Accepted != 1 {
		t.Errorf("cadence accepted = %d after the retry, want 1 (a re-dispatch advances nothing)", row.Accepted)
	}
}

// fenAttemptKey is the fence identity of a fen job: the same business key the
// engine's dispatch path builds, so the test reads the fence row the send
// actually wrote.
func fenAttemptKey(j *job.DeliveryJob) job.AttemptKey {
	return job.AttemptKey{
		RuleID:          j.RuleID,
		SourceChatID:    j.SourceChatID,
		SourceMessageID: j.SourceMessageID,
		TargetChatID:    j.TargetChatID,
		JobKind:         j.JobKind,
	}
}

// storeInsert writes the job row the way the run loop's enqueue would.
func storeInsert(t *testing.T, s queue.Store, j *job.DeliveryJob) {
	t.Helper()
	queue.InsertJob(t, s, j)
}

// TestFenOverMaxIntervalClampsToThousand: a projected value above the
// documented ceiling reads as 1000 on both engines, so a hand-edited row
// cannot silence the task (the clamp is the projection's, and the gate
// trusts the clamped projection).
func TestFenOverMaxIntervalClampsToThousand(t *testing.T) {
	h, _ := newFenHarness(t, 100000) // the projection clamps; the engine sees 1000
	// Message 1 owns slot 1 of a 1000-cycle; the next 998 do not.
	j := fenJob(1, map[string]any{"text": "m"})
	h.enqueue(t, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process 1: %v", err)
	}
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Errorf("gateway calls = %d, want 1 (the first message of the cycle)", got)
	}
	for messageID := int64(2); messageID <= 10; messageID++ {
		j := fenJob(messageID, map[string]any{"text": "m"})
		h.enqueue(t, j)
		if err := h.engine.Process(context.Background(), j); err != nil {
			t.Fatalf("process %d: %v", messageID, err)
		}
	}
	if got := len(h.gateway.Recorded()); got != 1 {
		t.Errorf("gateway calls = %d, want 1/9 under N=1000", got)
	}
}
