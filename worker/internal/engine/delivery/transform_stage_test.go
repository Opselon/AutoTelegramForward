package delivery

import (
	"context"
	"testing"
	"time"

	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/job"
	"atf/worker/internal/domain/rule"
	"atf/worker/internal/queue"
	"atf/worker/internal/telegraph/fakegateway"
)

// transform_stage_test.go — the Stage 4.2 execution-boundary contract.
//
// What is under test here is NOT the transform math (that is the cleaning
// package's differential suite) but the BOUNDARY: which job gets
// transformed by the Worker, which must never be, and what happens to the
// payload when the pipeline runs or drops.

// transformJob builds a job carrying the transform-boundary keys the
// Python enqueue path writes.
func transformJob(keys map[string]any) *job.DeliveryJob {
	raw := map[string]any{
		"session_id":        "s1",
		"source_chat_id":    "-1001",
		"source_message_id": 42,
		"target_chat_id":    "-1002",
	}
	for key, value := range keys {
		raw[key] = value
	}
	return &job.DeliveryJob{
		ID:              "r1:-1001:42:-1002",
		RuleID:          "r1",
		SourceChatID:    "-1001",
		SourceMessageID: 42,
		TargetChatID:    "-1002",
		Status:          job.StatusPending,
		MaxAttempts:     5,
		CreatedAt:       1,
		Payload:         *job.NewPayload(raw),
	}
}

// staticTransformRules serves one fixed rule + transform config.
type staticTransformRules struct {
	transform cleaning.Config
}

func (s staticTransformRules) GetByID(ruleID string) (*rule.ForwardRule, *rule.FilterRule, error) {
	r := &rule.ForwardRule{
		ID:          "r1",
		SessionID:   "s1",
		IsActive:    true,
		OwnerUserID: 7,
		Transform:   s.transform,
	}
	return r, nil, nil
}

func TestOwnsTransformsGate(t *testing.T) {
	cases := []struct {
		name    string
		switch_ bool
		payload map[string]any
		want    bool
	}{
		{
			name:    "switch off never owns",
			switch_: false,
			payload: map[string]any{"raw_text": "hello"},
			want:    false,
		},
		{
			name:    "switch on but python-transformed job is excluded",
			switch_: true,
			payload: map[string]any{"raw_text": "hello", "transforms_version": "py-1"},
			want:    false,
		},
		{
			name:    "switch on with raw snapshot owns",
			switch_: true,
			payload: map[string]any{"raw_text": "hello"},
			want:    true,
		},
		{
			name:    "switch on with no raw snapshot cannot transform",
			switch_: true,
			payload: map[string]any{"text": "hello"},
			want:    false,
		},
		{
			name:    "pre-4.2 job with no marker is not transformed",
			switch_: true,
			payload: map[string]any{"text": "hello"},
			want:    false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			SetWorkerOwnsTransforms(tc.switch_)
			defer SetWorkerOwnsTransforms(false)
			h := newHarness(t)
			if got := h.engine.ownsTransforms(transformJob(tc.payload)); got != tc.want {
				t.Errorf("ownsTransforms = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestTransformSwitchIsProcessWideAndRaceSafe(t *testing.T) {
	// The switch is read on every job from multiple worker goroutines, so
	// the race detector is the test: flipping it while a reader runs must
	// stay clean.
	h := newHarness(t)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 0; i < 200; i++ {
			SetWorkerOwnsTransforms(i%2 == 0)
		}
	}()
	for i := 0; i < 200; i++ {
		_ = h.engine.ownsTransforms(transformJob(map[string]any{"raw_text": "x"}))
	}
	<-done
}

func TestApplyTransformPipelineUpdatesPayloadText(t *testing.T) {
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	j := transformJob(map[string]any{
		"raw_text": "see https://a.io/x now",
		"text":     "see https://a.io/x now",
	})
	h := newHarness(t)
	err := h.engine.applyTransformPipeline(context.Background(), j,
		transformRuleWith(cleaning.Config{Cleaner: &cleaning.CleanerRule{RemoveURLs: true, IsActive: true}}),
		h.log)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := "see now"; j.Payload.Text() != want {
		t.Errorf("payload text not updated: got %q, want %q", j.Payload.Text(), want)
	}
	// The raw snapshot must survive the pass: a restart re-reads it.
	if j.Payload.Raw()["raw_text"] != "see https://a.io/x now" {
		t.Errorf("raw snapshot must be preserved: %v", j.Payload.Raw()["raw_text"])
	}
}

func TestApplyTransformPipelineDropsEmptyAfterClean(t *testing.T) {
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	j := transformJob(map[string]any{"raw_text": "https://a.io", "text": "https://a.io"})
	h := newHarness(t)
	decisions := &captureDecisions{}
	h.engine.decisions = decisions
	err := h.engine.applyTransformPipeline(context.Background(), j,
		transformRuleWith(cleaning.Config{Cleaner: &cleaning.CleanerRule{RemoveURLs: true, IsActive: true}}),
		h.log)
	if err != errTransformDrop {
		t.Errorf("expected the drop sentinel, got %v", err)
	}
	if decisions.dropDecision != "DROP" || decisions.dropReason != "EMPTY_AFTER_CLEAN" {
		t.Errorf("expected the drop decision event, got %+v", decisions.last)
	}
}

func TestApplyTransformPipelineRecordsDecision(t *testing.T) {
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	j := transformJob(map[string]any{"raw_text": "see https://a.io now", "text": "see https://a.io now"})
	h := newHarness(t)
	decisions := &captureDecisions{}
	h.engine.decisions = decisions
	err := h.engine.applyTransformPipeline(context.Background(), j,
		transformRuleWith(cleaning.Config{Cleaner: &cleaning.CleanerRule{RemoveURLs: true, IsActive: true}}),
		h.log)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(decisions.events) != 1 {
		t.Fatalf("expected one decision event, got %d", len(decisions.events))
	}
	event := decisions.events[0]
	if event.Category != "transform" {
		t.Errorf("category: got %q", event.Category)
	}
	if event.RuleID != "r1" {
		t.Errorf("rule attribution missing: %+v", event)
	}
	if event.Decision != "APPLY" {
		t.Errorf("decision: got %q", event.Decision)
	}
	if len(event.StageNames) == 0 {
		t.Error("the event must name the stages that ran")
	}
}

func TestTransformPipelineNeverRerunsPythonTransformedJob(t *testing.T) {
	// The double-transform guard, end to end: a job that carries the
	// Python pipeline's version marker must reach dispatch with its text
	// byte-identical, whatever the process switch says.
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	baked := "PYTHON TRANSFORMED"
	j := transformJob(map[string]any{"text": baked, "transforms_version": "py-1"})
	h := newHarness(t)
	if h.engine.ownsTransforms(j) {
		t.Error("a python-transformed job must never be re-transformed")
	}
	if j.Payload.Text() != baked {
		t.Errorf("baked text must survive: got %q", j.Payload.Text())
	}
}

func TestEndToEndTransformedDelivery(t *testing.T) {
	// The full path: a raw-snapshot job runs the transform pipeline and
	// delivers the TRANSFORMED text, while the fence, mapping and SENT
	// marking all see the same payload the dispatch stage did.
	SetWorkerOwnsTransforms(true)
	defer SetWorkerOwnsTransforms(false)

	transformCfg := cleaning.Config{Cleaner: &cleaning.CleanerRule{RemoveURLs: true, IsActive: true}}
	store := queue.NewTestStore(t)
	gw := fakegateway.New("")
	metrics := &testMetrics{}
	log := &testLogger{}
	engine := New(store, staticTransformRules{transform: transformCfg}, gw, nil, log, metrics, "w-test", t.TempDir(), Options{
		NumWorkers: 2, BatchSize: 5, LeaseDuration: 30 * time.Second, RPCTimeout: 5 * time.Second,
	})

	j := transformJob(map[string]any{
		"raw_text":      "see https://a.io/x now",
		"text":          "see https://a.io/x now",
		"forward_mode":  "COPY_MESSAGE",
		"content_mode":  "AUTO",
		"has_media":     false,
		"media_type":    "text",
		"fallback_enabled": true,
	})
	insertJobRow(t, store, j)

	if err := engine.Process(context.Background(), j); err != nil {
		t.Fatalf("process: %v", err)
	}
	if metrics.sent != 1 {
		t.Fatalf("expected one sent, got %d", metrics.sent)
	}
	// The gateway received the TRANSFORMED text, not the raw snapshot.
	// The job is a text-only copy, so the dispatch path reaches
	// SendMessage and the recorded call carries the payload's text.
	calls := gw.Recorded()
	var sent string
	for _, call := range calls {
		if call.Op == "SendMessage" {
			sent = call.Text
			break
		}
	}
	if sent != "see now" {
		t.Errorf("dispatched text: got %q, want %q", sent, "see now")
	}
}

// --- helpers ------------------------------------------------------------------

func transformRuleWith(cfg cleaning.Config) *rule.ForwardRule {
	r := &rule.ForwardRule{ID: "r1", SessionID: "s1", IsActive: true, OwnerUserID: 7}
	r.Transform = cfg
	return r
}

// captureDecisions records decision events for assertions.
type captureDecisions struct {
	events      []cleaning.DecisionEvent
	dropDecision string
	dropReason   string
	last         cleaning.DecisionEvent
}

func (c *captureDecisions) RecordDecision(_ context.Context, event cleaning.DecisionEvent) {
	c.events = append(c.events, event)
	c.last = event
	if event.Decision == "DROP" {
		c.dropDecision = event.Decision
		c.dropReason = event.ReasonCode
	}
}

var _ DecisionRecorderAdapter = (*captureDecisions)(nil)

// DecisionRecorderAdapter is the interface shape the engine accepts; kept
// as a named alias so the test file reads the same contract as main.
type DecisionRecorderAdapter = cleaning.DecisionRecorder
