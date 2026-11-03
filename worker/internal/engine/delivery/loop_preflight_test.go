package delivery

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph/fakegateway"
)

// --- Stage 4.5 W1: LOOP_SKIP preflight -------------------------------------- //

// routingHarness builds a delivery engine over a custom rule store, so the
// loop cases can serve the task shape each scenario needs (re-pointed
// target, forward-origin target, username target) without touching the
// shared harness's always-delivers default. It installs its OWN logger —
// one that keeps the structured args — because the shared testLogger drops
// them, and the reason_code IS the assertion.
func routingHarness(t *testing.T, r *rule.ForwardRule) *loopHarness {
	t.Helper()
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &argLogger{}
	engine := New(store, routingRule{r: r}, gw, nil, log, metrics, "w-routing",
		t.TempDir(),
		Options{NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second})
	return &loopHarness{store: store, gateway: gw, engine: engine, metrics: metrics, log: log}
}

// loopHarness is the routing tests' own handle: the shared harness pins a
// *testLogger that drops structured args, and the reason code IS the
// assertion here.
type loopHarness struct {
	store   queue.Store
	gateway *fakegateway.Fake
	engine  *Engine
	metrics *testMetrics
	log     *argLogger
}

// enqueue inserts the job through the harness's store, mirroring the
// shared harness's own helper.
func (h *loopHarness) enqueue(t *testing.T, j *job.DeliveryJob) {
	t.Helper()
	insertJobRow(t, h.store, j)
}

// argLogger is the shared testLogger's field-keeping twin: it appends the
// key/value pairs to the line, so a test can assert on the structured
// reason codes the engine emits alongside the message.
type argLogger struct{ lines []string }

func (l *argLogger) Info(msg string, args ...any) {
	l.lines = append(l.lines, "INFO "+msg+formatArgs(args))
}
func (l *argLogger) Warn(msg string, args ...any) {
	l.lines = append(l.lines, "WARN "+msg+formatArgs(args))
}
func (l *argLogger) Error(msg string, args ...any) {
	l.lines = append(l.lines, "ERROR "+msg+formatArgs(args))
}

// formatArgs renders the slog-style key/value tail of one record.
func formatArgs(args []any) string {
	var b strings.Builder
	for i := 0; i+1 < len(args); i += 2 {
		b.WriteString(" ")
		if k, ok := args[i].(string); ok {
			b.WriteString(k)
		}
		b.WriteString("=")
		fmt.Fprintf(&b, "%v", args[i+1])
	}
	return b.String()
}

// routingRule is a rule.Store answering with one fixed task projection.
type routingRule struct{ r *rule.ForwardRule }

func (s routingRule) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	if s.r != nil && s.r.ID == ruleID {
		return s.r, nil, nil
	}
	return nil, nil, nil
}

// routingJob is the job shape the loop cases enqueue: a text message from
// -1001, with the source chat id snapshotted into the payload the way the
// Python enqueue path snapshots it.
func routingJob(id, ruleID, target string, payload map[string]any) *job.DeliveryJob {
	extra := map[string]any{"text": "hello", "source_chat_id": "-1001"}
	for k, v := range payload {
		extra[k] = v
	}
	return &job.DeliveryJob{
		ID: id, RuleID: ruleID, SourceChatID: "-1001", SourceMessageID: 1,
		TargetChatID: target, Status: job.StatusPending, MaxAttempts: 5, CreatedAt: 1,
		Payload: *jobPayload(extra),
	}
}

// TestLoopSkipClosesSentWithoutSending: a target that would route the
// message back onto its own source is SKIPPED, not failed — the Python
// pipeline's LOOP_SKIP (pipeline.py:1226). The job closes
// SENT-without-send: the message is deliberately not delivered, and a
// failure would spend an attempt and retry a decision that never changes.
func TestLoopSkipClosesSentWithoutSending(t *testing.T) {
	h := routingHarness(t, &rule.ForwardRule{ID: "rloop", IsActive: true, SourceChatID: "-1001"})
	j := routingJob("rloop:-1001:1:-1001", "rloop", "-1001", nil)
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Errorf("sent = %d, want 1 (a LOOP_SKIP closes as sent, not failed)", h.metrics.sent)
	}
	if h.metrics.failed != 0 {
		t.Errorf("failed = %d, want 0 (a loop skip is never a failure)", h.metrics.failed)
	}
	if calls := h.gateway.Recorded(); len(calls) != 0 {
		t.Errorf("gateway calls = %d, want 0 (the looping target must not be sent to)", len(calls))
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
	// The decision must be visible in the log with the Python reason
	// vocabulary, so an operator sees the same words either engine emits.
	if !logHas(t, h.log.lines, "LOOP_SKIP", "target_matches_source") {
		t.Errorf("no LOOP_SKIP log line carrying target_matches_source; log = %v", h.log.lines)
	}
}

// TestLoopSkipDoesNotAffectOtherTargets is the contract the Python
// pipeline states as "a skip is not a filter": the same message, delivered
// by a task whose target is NOT the source, still goes through. The loop
// skip must never grow into a whole-job failure.
func TestLoopSkipDoesNotAffectOtherTargets(t *testing.T) {
	h := routingHarness(t, &rule.ForwardRule{ID: "rdist", IsActive: true, SourceChatID: "-1001"})
	j := routingJob("rdist:-1001:1:-2001", "rdist", "-2001", nil)
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Errorf("sent = %d, want 1 (the non-looping target still delivers)", h.metrics.sent)
	}
	if calls := h.gateway.Recorded(); len(calls) == 0 {
		t.Error("no gateway call was made for the non-looping target")
	}
	assertJobStatus(t, h.store, j.ID, string(job.StatusSent))
}

// TestLoopSkipForwardOrigin: the forward-origin arm — the target is the
// chat a forwarded message originally came from.
func TestLoopSkipForwardOrigin(t *testing.T) {
	h := routingHarness(t, &rule.ForwardRule{ID: "rorig", IsActive: true, SourceChatID: "-1001"})
	j := routingJob("rorig:-1001:1:-1099", "rorig", "-1099",
		map[string]any{"forward_origin": map[string]any{"from_chat_id": "-1099"}})
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Errorf("sent = %d, want 1 (LOOP_SKIP closes as sent)", h.metrics.sent)
	}
	if calls := h.gateway.Recorded(); len(calls) != 0 {
		t.Errorf("gateway calls = %d, want 0 (a forward-origin loop must not be sent)", len(calls))
	}
	if !logHas(t, h.log.lines, "LOOP_SKIP", "target_matches_forward_origin") {
		t.Errorf("no LOOP_SKIP line carrying target_matches_forward_origin; log = %v", h.log.lines)
	}
}

// TestLoopSkipUsernameArm: the source's @username equals the target —
// the spelling a numeric-id comparison alone would miss, and the arm the
// Python check_loop compares case-insensitively.
func TestLoopSkipUsernameArm(t *testing.T) {
	h := routingHarness(t, &rule.ForwardRule{ID: "ruser", IsActive: true, SourceChatID: "-1001"})
	j := routingJob("ruser:-1001:1:@samechannel", "ruser", "@samechannel",
		map[string]any{"source_chat_username": "@SameChannel"})
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Errorf("sent = %d, want 1 (LOOP_SKIP closes as sent)", h.metrics.sent)
	}
	if calls := h.gateway.Recorded(); len(calls) != 0 {
		t.Errorf("gateway calls = %d, want 0 (the username arm must not be sent)", len(calls))
	}
	if !logHas(t, h.log.lines, "LOOP_SKIP", "target_matches_source") {
		t.Errorf("no LOOP_SKIP line carrying target_matches_source; log = %v", h.log.lines)
	}
}

// TestLoopNotTriggeredForIntermediateTarget: the branding channel C as
// target is NOT a loop when the message came from elsewhere — the hop
// still runs, and the preflight must not preempt it.
func TestLoopNotTriggeredForIntermediateTarget(t *testing.T) {
	h := routingHarness(t, &rule.ForwardRule{
		ID: "rinter", IsActive: true, SourceChatID: "-1001", IntermediateChannelID: "-1300",
	})
	j := routingJob("rinter:-1001:1:-1300", "rinter", "-1300",
		map[string]any{"forward_origin": map[string]any{"from_chat_id": "-1099"}})
	h.enqueue(t, j)

	if err := h.engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if h.metrics.sent != 1 {
		t.Errorf("sent = %d, want 1 (the intermediate target still delivers)", h.metrics.sent)
	}
	if calls := h.gateway.Recorded(); len(calls) == 0 {
		t.Error("no gateway call was made for the intermediate target")
	}
}

// logHas reports whether one log line carries BOTH markers — the decision
// and its Python reason vocabulary.
func logHas(t *testing.T, lines []string, markers ...string) bool {
	t.Helper()
	for _, line := range lines {
		all := true
		for _, marker := range markers {
			if !containsAll(line, marker) {
				all = false
				break
			}
		}
		if all {
			return true
		}
	}
	return false
}

// containsAll is the substring test the log scanner above uses, kept local
// so this file does not reach into the shared test helpers' own helper.
func containsAll(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
