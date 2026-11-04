// Package opsstore_test also pins the ENGINE-level regression: /ops must
// report the counters the real delivery engine increments (the
// delivered=0 defect). Same package as the store tests, for the same
// reason: importing internal/queue from inside the ops package itself
// would be an import cycle (queue's store adapters import ops).
package opsstore_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/engine/delivery"
	"atf/worker/internal/ops"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph/fakegateway"
)

// engineHarness wires a REAL delivery engine against a real (SQLite) queue
// store and the fake gateway, exactly the way the worker composes it: the
// collector is BOTH the engine's Metrics AND its stage observer — the
// one-owner invariant the defect demanded.
type engineHarness struct {
	store     queue.Store
	engine    *delivery.Engine
	collector *ops.Collector
}

func newEngineHarness(t *testing.T) *engineHarness {
	t.Helper()
	store := queue.NewTestStore(t)
	collector := testCollectorOps()
	engine := delivery.New(store, staticTestRules{}, fakegateway.New(""), nil,
		&captureLogger{}, collector, "w-test", t.TempDir(),
		delivery.Options{
			NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second,
			RPCTimeout: 5 * time.Second,
		})
	engine.SetStageObserver(ops.NewEngineObserver(collector))
	return &engineHarness{store: store, engine: engine, collector: collector}
}

// staticTestRules: one live rule — the shape every delivery test needs.
type staticTestRules struct{}

func (staticTestRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	return &rule.ForwardRule{ID: ruleID, IsActive: true}, nil, nil
}

// captureLogger is a discard sink: this harness counts outcomes, it does
// not read logs. Failures surface as assertion failures instead.
type captureLogger struct{}

func (l *captureLogger) Info(string, ...any)  {}
func (l *captureLogger) Warn(string, ...any)  {}
func (l *captureLogger) Error(string, ...any) {}

// stageRowOf is the local twin of the ops package's test helper.
func stageRowOf(t *testing.T, c *ops.Collector, name string) map[string]any {
	t.Helper()
	for _, s := range c.Stages() {
		if s["stage"] == name {
			return s
		}
	}
	t.Fatalf("stage %q missing from Stages()", name)
	return nil
}

// jobID keeps the seeded fixture ids unique and readable.
func jobID(i int) string { return "engine-job-" + string(rune('a'+i)) }

// seedJob writes one CLAIMED job the engine can process immediately.
func seedJob(t *testing.T, h *engineHarness, i int) *job.DeliveryJob {
	t.Helper()
	j := &job.DeliveryJob{
		ID: jobID(i), RuleID: "ruleA",
		SourceChatID: "-1001", SourceMessageID: int64(i + 1),
		TargetChatID: "-2001", Status: job.StatusClaimed,
		MaxAttempts: 5, CreatedAt: time.Now().Unix(),
		JobKind: "forward",
	}
	j.Payload = *job.NewPayload(map[string]any{"session_id": "s1", "text": "hello"})
	queue.InsertJob(t, h.store, j)
	return j
}

// TestOpsReportsEngineDeliveredCount is THE regression test for the old
// defect. The engine processes real jobs through the real pipeline; /ops
// must then serve the count the engine itself incremented — the same
// counter, read at the HTTP boundary.
func TestOpsReportsEngineDeliveredCount(t *testing.T) {
	h := newEngineHarness(t)

	for i := 0; i < 5; i++ {
		j := seedJob(t, h, i)
		if err := h.engine.Process(context.Background(), j); err != nil {
			t.Fatalf("engine.Process %s: %v", j.ID, err)
		}
	}

	// Serve /ops from the same mux the worker attaches it to.
	srv := ops.NewServer(h.collector, h.store, h.store, nil, "sqlite", "w-test")
	mux := http.NewServeMux()
	srv.Attach(mux)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/ops")
	if err != nil {
		t.Fatalf("GET /ops: %v", err)
	}
	defer resp.Body.Close()
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode /ops: %v", err)
	}
	queueObj, _ := body["queue"].(map[string]any)
	if got := queueObj["delivered"]; got != float64(5) {
		t.Fatalf("/ops queue.delivered = %v, want 5 — /ops must report the SAME "+
			"delivered counter the engine increments (the delivered=0 defect)", got)
	}
	// And the health snapshot agrees — one number, two surfaces.
	if got := h.collector.Snapshot()["delivered"]; got != int64(5) {
		t.Fatalf("health snapshot delivered = %v, want 5", got)
	}
	// The delivery stage saw them all.
	row := stageRowOf(t, h.collector, "delivery")
	if got := row["processed"]; got != int64(5) {
		t.Fatalf("delivery stage processed = %v, want 5", got)
	}
	if got := row["health"]; got != ops.StageHealthy {
		t.Fatalf("delivery stage health = %v, want %q", got, ops.StageHealthy)
	}
}

// TestOpsEngineEndToEndLatencyIsRecorded: a processed job must leave an
// end-to-end latency observation — the rings must never read "no data" on
// a worker that is actively delivering.
func TestOpsEngineEndToEndLatencyIsRecorded(t *testing.T) {
	h := newEngineHarness(t)
	j := seedJob(t, h, 0)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("engine.Process: %v", err)
	}
	if n := h.collector.E2E().countInWindow(); n < 1 {
		t.Fatalf("end-to-end latency observations = %d, want ≥1 after a delivery", n)
	}
	if p := h.collector.E2E().percentile(50); p <= 0 {
		t.Fatalf("p50 after a delivery = %v, want > 0", p)
	}
	// The queue-wait ring is fed by the engine's ObserveWait.
	if n := h.collector.QueueWait().countInWindow(); n < 1 {
		t.Fatalf("queue-wait observations = %d, want ≥1", n)
	}
}

// TestOpsStagesInstrumentedAcrossEngine: the stages the engine actually
// owns must show processed counts; the full 11 are reported in the
// contract's order whatever their state.
func TestOpsStagesInstrumentedAcrossEngine(t *testing.T) {
	h := newEngineHarness(t)
	j := seedJob(t, h, 0)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("engine.Process: %v", err)
	}

	// The stages a claim-deliver job must have touched.
	for _, stage := range []string{"task_resolution", "queue", "delivery"} {
		row := stageRowOf(t, h.collector, stage)
		if got := row["processed"].(int64); got < 1 {
			t.Errorf("stage %q processed = %d, want ≥1", stage, got)
		}
	}

	// The full 11 are reported, in the contract's fixed order.
	full := h.collector.Stages()
	if len(full) != len(ops.StageNames) {
		t.Fatalf("Stages() = %d entries, want %d", len(full), len(ops.StageNames))
	}
	for i, name := range ops.StageNames {
		if got := full[i]["stage"]; got != name {
			t.Fatalf("stage %d = %v, want %q (order is the contract)", i, got, name)
		}
	}
}

// TestOpsReportsFailedJobThroughEngine: a failed delivery must show up as
// BOTH a failed counter AND a stage failure — so the panel never paints
// the stage green while the queue is failing jobs.
func TestOpsReportsFailedJobThroughEngine(t *testing.T) {
	h := newEngineHarness(t)
	// A rule that does not exist: the engine drops the job FAILED through
	// the task_resolution preflight (RULE_NOT_FOUND).
	j := &job.DeliveryJob{
		ID: "gone", RuleID: "rule-gone",
		SourceChatID: "-1001", SourceMessageID: 42, TargetChatID: "-2001",
		Status: job.StatusClaimed, MaxAttempts: 5,
		CreatedAt: time.Now().Unix(), JobKind: "forward",
	}
	j.Payload = *job.NewPayload(map[string]any{"session_id": "s1", "text": "hello"})
	queue.InsertJob(t, h.store, j)
	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("engine.Process: %v", err)
	}
	if got := h.collector.Failed(); got != 1 {
		t.Fatalf("failed counter = %d, want 1", got)
	}
	row := stageRowOf(t, h.collector, "task_resolution")
	if got := row["failures"]; got != int64(1) {
		t.Fatalf("task_resolution failures = %v, want 1", got)
	}
	if got := row["health"]; got != ops.StageFailed {
		t.Fatalf("task_resolution health = %v, want %q", got, ops.StageFailed)
	}
}
