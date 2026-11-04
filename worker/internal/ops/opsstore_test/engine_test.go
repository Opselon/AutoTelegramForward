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

// staticTestRules resolves every id to one live rule — the shape the
// happy-path delivery tests need. The resolution-less (RULE_NOT_FOUND)
// path has its own rule store: that decision is a *test* fixture, not a
// shortcut — a store that resolves "rule-gone" to a live rule makes the
// drop path unreachable, and a regression test that can never fire is
// worse than none at all.
type staticTestRules struct{}

func (staticTestRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	return &rule.ForwardRule{ID: ruleID, IsActive: true}, nil, nil
}

// missingRuleRules resolves only the live rule; every other id is
// unknown to the store, which is the engine's signal to drop the job
// FAILED through the task_resolution preflight (RULE_NOT_FOUND).
type missingRuleRules struct{ liveID string }

func (m missingRuleRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	if ruleID == m.liveID {
		return &rule.ForwardRule{ID: ruleID, IsActive: true}, nil, nil
	}
	return nil, nil, nil
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

// newEngineHarnessWithRules wires the same real engine against an
// explicit rule store, for the paths a static fixture cannot reach
// (RULE_NOT_FOUND, RULE_DISABLED).
func newEngineHarnessWithRules(t *testing.T, rules rule.Store) *engineHarness {
	t.Helper()
	store := queue.NewTestStore(t)
	collector := testCollectorOps()
	engine := delivery.New(store, rules, fakegateway.New(""), nil,
		&captureLogger{}, collector, "w-test", t.TempDir(),
		delivery.Options{
			NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second,
			RPCTimeout: 5 * time.Second,
		})
	engine.SetStageObserver(ops.NewEngineObserver(collector))
	return &engineHarness{store: store, engine: engine, collector: collector}
}

// seedJobWithRule plants one CLAIMED job under an explicit rule id, for
// the drop-path fixtures that cannot reuse the static fixture's rule id.
func seedJobWithRule(t *testing.T, h *engineHarness, id, ruleID string) *job.DeliveryJob {
	t.Helper()
	j := &job.DeliveryJob{
		ID: id, RuleID: ruleID,
		SourceChatID: "-1001", SourceMessageID: 42, TargetChatID: "-2001",
		Status: job.StatusClaimed, MaxAttempts: 5,
		CreatedAt: time.Now().Unix(), JobKind: "forward",
	}
	j.Payload = *job.NewPayload(map[string]any{"session_id": "s1", "text": "hello"})
	queue.InsertJob(t, h.store, j)
	return j
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
	var queueReader ops.QueueStoreReader
	var taskReader ops.TaskStoreReader
	if r, ok := h.store.(ops.QueueStoreReader); ok {
		queueReader = r
	}
	if r, ok := h.store.(ops.TaskStoreReader); ok {
		taskReader = r
	}
	srv := ops.NewServer(h.collector, queueReader, taskReader, nil, "sqlite", "w-test")
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
	if n := h.collector.E2ECount(); n < 1 {
		t.Fatalf("end-to-end latency observations = %d, want ≥1 after a delivery", n)
	}
	if p := h.collector.E2EPercentile(50); p <= 0 {
		t.Fatalf("p50 after a delivery = %v, want > 0", p)
	}
	// The queue-wait ring is fed by the engine's ObserveWait.
	if n := h.collector.QueueWaitCount(); n < 1 {
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
//
// The path exercised is the task_resolution preflight DROP: a job whose
// rule no longer exists. The harness needs a rule store that genuinely
// fails the lookup — a store that resolves every id to a live rule makes
// this path unreachable, and the assertion would then be pinning
// nothing.
func TestOpsReportsFailedJobThroughEngine(t *testing.T) {
	h := newEngineHarnessWithRules(t, missingRuleRules{liveID: "ruleA"})
	seedJobWithRule(t, h, "gone", "rule-gone")

	// The lookup is a DROP, not an error: Process returns nil.
	if err := h.engine.Process(context.Background(), &job.DeliveryJob{ID: "gone"}); err != nil {
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

	// The DROP must also land in the durable store: the /ops surface reads
	// its queue counts from delivery_jobs, and a failure that never reached
	// the row would make /ops/queue disagree with the counters.
	stats, err := h.store.(ops.QueueStoreReader).QueueStats(context.Background())
	if err != nil {
		t.Fatalf("QueueStats: %v", err)
	}
	if stats.Failed+stats.DeadLetter < 1 {
		t.Fatalf("queue failed+dead_letter = %d, want ≥1 — the DROP must reach the durable row the /ops surface reads", stats.Failed+stats.DeadLetter)
	}
}
