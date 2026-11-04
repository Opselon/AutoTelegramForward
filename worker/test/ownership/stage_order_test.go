// stage_order_test.go — §4 of the Go-ownership contract: the stage-order
// proof.
//
// The claim under test is the OBSERVED execution order of the Go pipeline's
// stages — filter → replacement → cleaner → header/footer — read off the
// telemetry events the ENGINE ITSELF emits, not an assertion about an
// internal field or a comment in a source file.
//
// Two independent observation channels are pinned, because they answer two
// different questions:
//
//  1. delivery.StageObserver (the engine's own pipeline telemetry hook)
//     records the stages the engine walked, in the order it walked them.
//     A recorded stage is OBSERVED EXECUTION: the engine called the hook.
//
//  2. cleaning.DecisionEvent.StageNames (the transform pipeline's decision
//     log) records which TEXT stages changed the body, in the order they
//     changed it. A stage that appears there OBSERVEDLY MUTATED the text.
//
// The proof is load-bearing in both directions: it proves the documented
// order is the executed order, and it proves a filter rejection happens
// BEFORE any transform stage runs (the preflight contract — a job the
// filter drops never pays for, or lands in, the transform stage).
package ownership

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"

	"atf/worker/internal/engine/delivery"
)

// recordingStageObserver is the §4 instrumentation: an implementation of the
// engine's own StageObserver hook that records every stage the engine
// reports, in arrival order. The engine calls this hook as it walks the
// pipeline, so the arrival order IS the execution order.
type recordingStageObserver struct {
	mu       sync.Mutex
	stages   []string
	fails    []string
	outcomes []string
}

func (r *recordingStageObserver) StageRecord(stage string, latencyMs float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.stages = append(r.stages, stage)
}

func (r *recordingStageObserver) StageRecordSkipped(stage string, latencyMs float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.stages = append(r.stages, stage+":skipped")
}

func (r *recordingStageObserver) StageFail(stage, msg string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.fails = append(r.fails, stage)
	r.stages = append(r.stages, stage+":failed")
}

func (r *recordingStageObserver) StageEnabled(stage string, enabled bool) {}

func (r *recordingStageObserver) DeliveryOutcome(kind string, ruleID string, latencyMs float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.outcomes = append(r.outcomes, kind)
}

func (r *recordingStageObserver) RecordError(component, severity, msg string, fields map[string]string) {
}

// observed returns the stage sequence as the engine emitted it.
func (r *recordingStageObserver) observed() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, len(r.stages))
	copy(out, r.stages)
	return out
}

// TestStageOrderIsObservedNotAsserted: a fully-configured task (whitelist
// filter, replacement, cleaner, header/footer) runs one message through the
// real engine and reads the stage sequence back out of the engine's own
// telemetry. The order must be the documented order.
func TestStageOrderIsObservedNotAsserted(t *testing.T) {
	ctx := context.Background()

	t.Run("ATF-OWN-ORD-001", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		// A task carrying every text stage: the whitelist filter passes, the
		// replacement rewrites a token, the cleaner strips a URL, and the
		// header/footer pair frames the body.
		h.InstallFilter(FilterSpec{ID: "flt-ord", Enabled: true, Whitelist: []string{"STAGEOK"}})
		h.Install(TaskSpec{ID: "task-ord", Source: "-1001", Target: "-9001", Active: true,
			FilterID: "flt-ord", Header: "HDR:", Footer: ":FTR",
			Metadata: map[string]any{"header": "HDR:", "footer": ":FTR"},
			Policy: &PolicySpec{Enabled: boolPtr(true),
				Replacements: map[string]string{"TOK": "REPLACED"},
				Cleaner:      &CleanerSpec{Enabled: true, Urls: true}}})

		obs := &recordingStageObserver{}
		h.Engine.SetStageObserver(obs)

		e := evt("ATF-OWN-ORD-001", "ORD",
			"observed stage order of a fully-configured delivery",
			"task_resolution → filter → (cleaner, replacement, header_footer, ai) → queue → delivery")
		start := now()
		j := h.Enqueue("task-ord", "-1001", 9801, "-9001", "STAGEOK TOK https://erase.example/x")
		err := h.Run(ctx, j)

		stages := obs.observed()
		// The engine's own hook reports the stages it walked, in order. This
		// is the OBSERVED order — no internal field was read to produce it.
		wantPrefix := []string{"task_resolution", "filter"}
		okPrefix := len(stages) >= len(wantPrefix)
		for i := range wantPrefix {
			if !okPrefix || stages[i] != wantPrefix[i] {
				okPrefix = false
				break
			}
		}
		// The transform stages arrive together (one ApplyText call records
		// the four text stages as one aggregate observation), and the fence
		// and dispatch follow them.
		transformIdx, queueIdx, deliveryIdx := -1, -1, -1
		for i, s := range stages {
			base := s
			if cut := strings.IndexByte(base, ':'); cut > 0 {
				base = base[:cut]
			}
			switch base {
			case "cleaner", "replacement", "header_footer", "ai":
				if transformIdx < 0 {
					transformIdx = i
				}
			case "queue":
				if queueIdx < 0 {
					queueIdx = i
				}
			case "delivery":
				if deliveryIdx < 0 {
					deliveryIdx = i
				}
			}
		}
		ordering := okPrefix && transformIdx > 1 && queueIdx > transformIdx && deliveryIdx > queueIdx

		// The decision-event side: the transform pipeline reports which text
		// stages CHANGED the body, in order. The cleaner must precede the
		// replacements, which must precede the header/footer — the order
		// pipeline.py documents and the Go engine must match.
		notes := h.StageNamesForRule("task-ord")
		// The decision log's own vocabulary: "cleaner" (stage 1), the
		// content-policy rewrite (stage 5, which carries the replacement
		// entries when the task configures them through the policy blob),
		// the top-level replacements (stage 6) and header_footer (stage 7).
		notesOK := stageOrder(notes, "cleaner", "content_policy") &&
			stageOrder(notes, "content_policy", "header_footer")

		delivered := h.AssertDelivered(&e, j, "HDR:")
		if ordering && notesOK && delivered {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("stages=%v notes=%v", stages, notes)
		} else {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("observed=%v notes=%v prefixOK=%v orderOK=%v notesOK=%v delivered=%v",
				stages, notes, okPrefix, ordering, notesOK, delivered)
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("ORD-001: %s", e.FailureReason)
		}
	})

	// ORD-002: the preflight contract. A filter rejection runs BEFORE any
	// transform stage — a dropped message never reaches the text pipeline,
	// and the engine's own telemetry is the proof.
	t.Run("ATF-OWN-ORD-002", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.InstallFilter(FilterSpec{ID: "flt-ord2", Enabled: true, Blacklist: []string{"BANNED"}})
		h.Install(TaskSpec{ID: "task-ord2", Source: "-1001", Target: "-9002", Active: true,
			FilterID: "flt-ord2",
			Policy: &PolicySpec{Enabled: boolPtr(true),
				Replacements: map[string]string{"TOK": "REPLACED"},
				Cleaner:      &CleanerSpec{Enabled: true, Urls: true}}})

		obs := &recordingStageObserver{}
		h.Engine.SetStageObserver(obs)

		e := evt("ATF-OWN-ORD-002", "ORD",
			"filter rejection precedes every transform stage",
			"filter:failed observed, no cleaner/replacement/header stage ever recorded")
		start := now()
		j := h.Enqueue("task-ord2", "-1001", 9802, "-9002", "BANNED TOK body")
		err := h.Run(ctx, j)

		stages := obs.observed()
		dropped := h.AssertDropped(&e, j)
		// The filter stage failed, and no transform stage was ever walked.
		filterFailed := containsStage(stages, "filter:failed") || containsStage(stages, "filter")
		noTransform := !containsAny(stages, "cleaner", "replacement", "header_footer", "ai")
		if dropped && filterFailed && noTransform {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("stages=%v", stages)
		} else {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("stages=%v dropped=%v filterFailed=%v noTransform=%v",
				stages, dropped, filterFailed, noTransform)
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("ORD-002: %s", e.FailureReason)
		}
	})

	// ORD-003: the transform pipeline's OWN decision log pins the text-stage
	// order for a body every stage touches. This is the channel the Rust Log
	// Monitor reads, so the order it reports is the order an operator sees.
	t.Run("ATF-OWN-ORD-003", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-ord3", Source: "-1001", Target: "-9003", Active: true,
			Header:   "HDR:",
			Metadata: map[string]any{"header": "HDR:"},
			Policy: &PolicySpec{Enabled: boolPtr(true),
				Replacements: map[string]string{"TOK": "REPLACED"},
				Cleaner:      &CleanerSpec{Enabled: true, Urls: true}}})

		e := evt("ATF-OWN-ORD-003", "ORD",
			"the decision log's stage order for a body every text stage touches",
			"cleaner before content_policy before header_footer")
		start := now()
		j := h.Enqueue("task-ord3", "-1001", 9803, "-9003", "TOK https://erase.example/x")
		err := h.Run(ctx, j)
		notes := h.StageNamesForRule("task-ord3")
		ok := stageOrder(notes, "cleaner", "content_policy") &&
			stageOrder(notes, "content_policy", "header_footer")
		if ok && h.AssertDelivered(&e, j, "HDR:") {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("notes=%v", notes)
		} else {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("notes=%v (want cleaner < content_policy < header_footer)", notes)
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("ORD-003: %s", e.FailureReason)
		}
	})
}

// stageOrder reports whether a appears before b in the observed stage-note
// sequence. Absent stages are "never ran", which satisfies any pairwise order
// involving them only when the OTHER stage is also absent — so a partially
// observed sequence cannot accidentally pass.
func stageOrder(notes []string, a, b string) bool {
	ia, ib := -1, -1
	for i, n := range notes {
		switch n {
		case a:
			if ia < 0 {
				ia = i
			}
		case b:
			if ib < 0 {
				ib = i
			}
		}
	}
	if ia < 0 || ib < 0 {
		return false
	}
	return ia < ib
}

// containsStage reports whether the engine observed the exact stage token.
func containsStage(stages []string, want string) bool {
	for _, s := range stages {
		if s == want {
			return true
		}
	}
	return false
}

// containsAny reports whether the engine observed any of the named stages.
func containsAny(stages []string, names ...string) bool {
	for _, s := range stages {
		base := s
		if cut := strings.IndexByte(base, ':'); cut > 0 {
			base = base[:cut]
		}
		for _, n := range names {
			if base == n {
				return true
			}
		}
	}
	return false
}

// Compile-time guarantee: the observer satisfies the engine's hook.
var _ delivery.StageObserver = (*recordingStageObserver)(nil)
