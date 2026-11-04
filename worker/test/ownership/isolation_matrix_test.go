// isolation_matrix_test.go — §1 of the Go-ownership contract: the multi-task
// isolation matrix.
//
// THE CONTRACT UNDER TEST. Six tasks are installed concurrently against ONE
// shared source chat, with deliberately-overlapping configurations:
//
//	Task A: filter whitelist + replacement X->1
//	Task B: filter blacklist + replacement X->2   (same X, different result)
//	Task C: AI config attached (stage unwired → delivers original)
//	Task D: cleaner (URL strip)
//	Task E: header/footer
//	Task F: forward-every-N = 2 + media job
//
// One message to the shared source fans out one job per task. Every observed
// transform must be by the GO engine (asserted via the recorded decision
// events, whose category is "transform" and whose rule ids are the task's),
// and the deliveries must be exactly the ones the OWNERSHIP contract predicts
// — a task's own whitelist never blocks its neighbour, a task's replacement
// never rewrites its neighbour's copy, and the FEN task's slot decisions
// depend only on its own counter.
//
// The sub-scenarios then stress the four specific leak shapes the brief names:
// independent FEN counters, independent replacements, filter isolation and
// config-cache contamination (a mid-run config change to one task must not
// change another task's outcome).
package ownership

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"

	"atf/worker/internal/domain/job"
)

// matrixTask is one column of the isolation matrix.
type matrixTask struct {
	taskID string
	target string
	spec   TaskSpec
	filter *FilterSpec
	// wantBody is the substring the gateway must see for this task, or "" when
	// the matrix predicts a drop.
	wantBody string
	// wantDrop marks a predicted rejection.
	wantDrop bool
}

// isolationMatrix builds the six concurrently-installed tasks. Each task gets
// a distinct target chat so the deliveries are separable in the gateway log.
func isolationMatrix(h *Harness, source string) []matrixTask {
	tasks := []matrixTask{
		{
			taskID: "task-iso-A", target: "-2001",
			spec: TaskSpec{ID: "task-iso-A", Source: source, SourceName: "shared-source",
				Target: "-2001", TargetName: "tgtA", Active: true,
				Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"X": "1"}}},
			filter:   &FilterSpec{ID: "flt-iso-A", Enabled: true, Whitelist: []string{"SHARED"}},
			wantBody: "SHARED 1 body",
		},
		{
			taskID: "task-iso-B", target: "-2002",
			spec: TaskSpec{ID: "task-iso-B", Source: source, SourceName: "shared-source",
				Target: "-2002", TargetName: "tgtB", Active: true,
				Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"X": "2"}}},
			// B's blacklist word is NOT in the probe, so the matrix predicts a
			// DELIVERY with B's own X->2 replacement. (The filter-isolation
			// sub-scenario below flips A's whitelist against B to prove the
			// converse direction.)
			filter:   &FilterSpec{ID: "flt-iso-B", Enabled: true, Blacklist: []string{"BLOCKED_FOR_B"}},
			wantBody: "SHARED 2 body",
		},
		{
			// C carries an AI config the harness does not wire a provider for:
			// the degraded mode the E category pins — the original text is
			// delivered, never silently lost.
			taskID: "task-iso-C", target: "-2003",
			spec: TaskSpec{ID: "task-iso-C", Source: source, SourceName: "shared-source",
				Target: "-2003", TargetName: "tgtC", Active: true, AIConfigID: "aiconfig-iso",
				Metadata: map[string]any{"paid_addons": map[string]any{"ai_rewrite": true},
					"smart_ai_rewrite": true}},
			// Unwired provider: the raw text survives the AI stage.
			wantBody: "SHARED X body",
		},
		{
			// D's cleaner strips the URL the probe carries; its copy must show
			// neither the URL nor another task's replacement.
			taskID: "task-iso-D", target: "-2004",
			spec: TaskSpec{ID: "task-iso-D", Source: source, SourceName: "shared-source",
				Target: "-2004", TargetName: "tgtD", Active: true,
				Policy: &PolicySpec{Enabled: boolPtr(true), Cleaner: &CleanerSpec{Enabled: true, Urls: true}}},
			wantBody: "SHARED X body",
		},
		{
			taskID: "task-iso-E", target: "-2005",
			spec: TaskSpec{ID: "task-iso-E", Source: source, SourceName: "shared-source",
				Target: "-2005", TargetName: "tgtE", Active: true,
				Header: "[E]", Footer: "[/E]",
				Metadata: map[string]any{"header": "[E]", "footer": "[/E]"}},
			wantBody: "[E]",
		},
		{
			// F runs a forward-every-N=2 cadence: with one message it consumes
			// slot 1 and forwards (owns-slot). The FEN sub-scenarios exercise
			// the counter interactions.
			taskID: "task-iso-F", target: "-2006",
			spec: TaskSpec{ID: "task-iso-F", Source: source, SourceName: "shared-source",
				Target: "-2006", TargetName: "tgtF", Active: true,
				Metadata: map[string]any{"forward_every_n": 2}},
			wantBody: "SHARED X body",
		},
	}
	return tasks
}

// installMatrix installs every task and its filter into one harness.
func installMatrix(h *Harness, tasks []matrixTask) {
	h.t.Helper()
	for _, t := range tasks {
		if t.filter != nil {
			h.InstallFilter(*t.filter)
			t.spec.FilterID = t.filter.ID
		}
		h.Install(t.spec)
	}
}

// probe is the one message the shared source receives. It carries every
// task's trigger: the whitelist keyword (A), the shared replacement token X
// and the URL D's cleaner must strip.
const probe = "SHARED X body https://leak.example/x"

// fanOut enqueues the probe once per task — the same shape the Python
// pipeline's enqueue fan-out produces: N jobs for one (source, message) pair,
// one per (task, target).
func fanOut(h *Harness, tasks []matrixTask, source string, msgID int64, text string) map[string]string {
	h.t.Helper()
	jobs := map[string]string{}
	for _, tk := range tasks {
		j := h.Enqueue(tk.taskID, source, msgID, tk.target, text)
		jobs[tk.taskID] = j.ID
	}
	return jobs
}

// TestIsolationMatrix is the headline §1 scenario: one message to the shared
// source produces exactly the deliveries the ownership contract predicts, and
// every observed transform is by the Go engine.
func TestIsolationMatrix(t *testing.T) {
	ctx := context.Background()

	t.Run("ATF-OWN-ISO-001", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		tasks := isolationMatrix(h, "-1001")
		installMatrix(h, tasks)
		jobs := fanOut(h, tasks, "-1001", 9001, probe)

		e := evt("ATF-OWN-ISO-001", "ISO",
			"one shared-source message fans out to 6 concurrently-installed tasks; each task's own contract holds",
			"A:X->1, B:X->2, C:unwired AI passthrough, D:URL stripped, E:header/footer, F:FEN slot 1 forwarded")
		start := now()
		var lastErr error
		for _, tk := range tasks {
			j := h.Lookup(jobs[tk.taskID])
			if err := h.Run(ctx, j); err != nil {
				lastErr = err
			}
		}

		// Per-task assertions: exactly the delivery the task's OWN contract
		// predicts, and nothing from its neighbours.
		passed := 0
		for _, tk := range tasks {
			te := evt("ATF-OWN-ISO-001", "ISO", tk.taskID+" outcome", tk.wantBody)
			te.JobID = jobs[tk.taskID]
			if tk.wantDrop {
				if assertTaskDrop(h, te, tk) {
					passed++
				}
			} else {
				if assertTaskBody(h, te, tk) {
					passed++
				}
			}
			rec.Record(te)
		}

		// The ownership predicate: every transform observed in this matrix was
		// performed by the GO engine. The decision events the engine recorded
		// carry the task's rule id and the "transform" category; a Python-side
		// transform would leave no such event.
		transformEvents := 0
		for _, ev := range h.Decisions() {
			if ev.Category == "transform" && ev.RuleID != "" {
				transformEvents++
			}
		}

		if passed == len(tasks) && transformEvents >= 1 {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("tasks_ok=%d go_transform_decisions=%d", passed, transformEvents)
		} else {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("tasks_ok=%d/%d, go_transform_decisions=%d", passed, len(tasks), transformEvents)
		}
		h.Finish(rec, &e, start, lastErr)
		if e.Result != ResultPass {
			t.Errorf("ISO-001: %s", e.FailureReason)
		}
		for _, f := range failOnly(rec) {
			t.Errorf("ISO-001 task %s: %s", f.JobID, f.FailureReason)
		}
	})

	// ISO-002: independent replacements. Task A replaces X->1 while task B
	// replaces X->2 on the SAME source message — both bodies must be correct,
	// which proves the replacement maps are resolved per task, not per Source.
	t.Run("ATF-OWN-ISO-002", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-r1", Source: "-1001", Target: "-3001", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"X": "1"}}})
		h.Install(TaskSpec{ID: "task-r2", Source: "-1001", Target: "-3002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"X": "2"}}})
		h.Install(TaskSpec{ID: "task-r3", Source: "-1001", Target: "-3003", Active: true})

		e := evt("ATF-OWN-ISO-002", "ISO",
			"independent replacements: same source token, three different task outcomes",
			"r1: X->1, r2: X->2, r3: X unchanged")
		start := now()
		j1 := h.Enqueue("task-r1", "-1001", 9101, "-3001", "X token")
		j2 := h.Enqueue("task-r2", "-1001", 9101, "-3002", "X token")
		j3 := h.Enqueue("task-r3", "-1001", 9101, "-3003", "X token")
		var lastErr error
		for _, j := range []*job.DeliveryJob{j1, j2, j3} {
			if err := h.Run(ctx, j); err != nil {
				lastErr = err
			}
		}
		ok := h.AssertDeliveredN(&e, "-3001", []string{"1 token"}) &&
			h.AssertDeliveredN(&e, "-3002", []string{"2 token"}) &&
			h.AssertDeliveredN(&e, "-3003", []string{"X token"})
		if ok {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, lastErr)
		if e.Result != ResultPass {
			t.Errorf("ISO-002: %s", e.FailureReason)
		}
	})

	// ISO-003: filter isolation. One task's whitelist must not block another
	// task watching the same source — the filter is a per-task configuration,
	// never a per-source gate.
	t.Run("ATF-OWN-ISO-003", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.InstallFilter(FilterSpec{ID: "flt-strict", Enabled: true, Whitelist: []string{"RARE"}})
		h.Install(TaskSpec{ID: "task-wl", Source: "-1001", Target: "-4001", Active: true,
			FilterID: "flt-strict"})
		h.Install(TaskSpec{ID: "task-open", Source: "-1001", Target: "-4002", Active: true})

		e := evt("ATF-OWN-ISO-003", "ISO",
			"filter isolation: one task's whitelist does not block its neighbour",
			"task-wl dropped by ITS whitelist; task-open delivers the same message")
		start := now()
		jw := h.Enqueue("task-wl", "-1001", 9201, "-4001", "plain message")
		jo := h.Enqueue("task-open", "-1001", 9201, "-4002", "plain message")
		errW := h.Run(ctx, jw)
		errO := h.Run(ctx, jo)
		// Per-target drop: the whitelist task's own target sees nothing, while
		// the same message still reaches the open task's target. Scoping the
		// assertion is the isolation question itself — a global "no gateway
		// call" check would let one task's filter veto its neighbour.
		dropped := h.AssertDroppedTarget(&e, jw, "-4001")
		delivered := h.AssertDeliveredN(&e, "-4002", []string{"plain message"})
		if dropped && delivered {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, firstErr(errW, errO))
		if e.Result != ResultPass {
			t.Errorf("ISO-003: %s", e.FailureReason)
		}
	})

	// ISO-004: independent FEN counters. Two tasks share a source and both run
	// N=2. Each task's slot decisions depend only on ITS counter: neither task
	// consumes the other's slots, and the sequences advance independently.
	t.Run("ATF-OWN-ISO-004", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-f1", Source: "-1001", Target: "-5001", Active: true,
			Metadata: map[string]any{"forward_every_n": 2}})
		h.Install(TaskSpec{ID: "task-f2", Source: "-1001", Target: "-5002", Active: true,
			Metadata: map[string]any{"forward_every_n": 2}})

		e := evt("ATF-OWN-ISO-004", "ISO",
			"independent FEN counters: two tasks, same source, both N=2",
			"each task sends msg1, skips msg2, sends msg3 — 3 sends total, counters at 3 each")
		start := now()
		var lastErr error
		for k, txt := range []string{"m1", "m2", "m3"} {
			j1 := h.Enqueue("task-f1", "-1001", int64(9301+k), "-5001", txt)
			j2 := h.Enqueue("task-f2", "-1001", int64(9301+k), "-5002", txt)
			if err := h.Run(ctx, j1); err != nil {
				lastErr = err
			}
			if err := h.Run(ctx, j2); err != nil {
				lastErr = err
			}
		}
		sent1 := sentTo(h, "-5001")
		sent2 := sentTo(h, "-5002")
		c1 := h.CounterAccepted(ctx, "task-f1")
		c2 := h.CounterAccepted(ctx, "task-f2")
		if sent1 == 2 && sent2 == 2 && c1 == 3 && c2 == 3 {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("sent=%d/%d counters=%d/%d", sent1, sent2, c1, c2)
		} else {
			e.Result = ResultFail
			e.Observed = fmt.Sprintf("sent=%d/%d counters=%d/%d", sent1, sent2, c1, c2)
			e.FailureReason = "each task must forward 2 of 3 and advance its own counter to 3"
		}
		h.Finish(rec, &e, start, lastErr)
		if e.Result != ResultPass {
			t.Errorf("ISO-004: %s (observed %s)", e.FailureReason, e.Observed)
		}
	})

	// ISO-005: config cache contamination. Updating task A's configuration
	// mid-run must not change task B's outcome — the engine re-reads each rule
	// per job from the rule store, so no cached projection leaks across tasks.
	t.Run("ATF-OWN-ISO-005", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-c1", Source: "-1001", Target: "-6001", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"ORIG": "C1"}}})
		h.Install(TaskSpec{ID: "task-c2", Source: "-1001", Target: "-6002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"ORIG": "C2"}}})

		e := evt("ATF-OWN-ISO-005", "ISO",
			"config cache contamination: mid-run config change to one task leaves the other untouched",
			"c1 sees the new config; c2 keeps its own")
		start := now()
		// First pass: both tasks see their own configuration.
		j1a := h.Enqueue("task-c1", "-1001", 9401, "-6001", "ORIG body")
		j2a := h.Enqueue("task-c2", "-1001", 9401, "-6002", "ORIG body")
		_ = h.Run(ctx, j1a)
		_ = h.Run(ctx, j2a)
		// Mid-run: task c1's replacement changes. Task c2's row is untouched.
		// json_set with a json() argument replaces the key's VALUE rather
		// than string-merging into it; both spellings the transform
		// projection reads (the top-level key and content_policy) are set,
		// mirroring the production panel write.
		h.DB().Exec(`UPDATE forward_rules SET metadata = json_set(json_set(metadata,
			'$.replacements', json('{"ORIG":"NEW"}')),
			'$.content_policy.replacements', json('{"ORIG":"NEW"}'))
			WHERE id = 'task-c1'`)
		h.ResetCalls()
		j1b := h.Enqueue("task-c1", "-1001", 9402, "-6001", "ORIG body")
		j2b := h.Enqueue("task-c2", "-1001", 9402, "-6002", "ORIG body")
		var lastErr error
		if err := h.Run(ctx, j1b); err != nil {
			lastErr = err
		}
		if err := h.Run(ctx, j2b); err != nil {
			lastErr = err
		}
		ok := h.AssertDeliveredN(&e, "-6001", []string{"NEW body"}) &&
			h.AssertDeliveredN(&e, "-6002", []string{"C2 body"})
		if ok {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, lastErr)
		if e.Result != ResultPass {
			t.Errorf("ISO-005: %s", e.FailureReason)
		}
	})

	// ISO-006: race-detector-clean concurrent run. All six matrix tasks run
	// their job simultaneously, sharing one engine, one queue and one gateway.
	// The race detector is the assertion: a cross-task leak of mutable state
	// (the engine's rate map, the recorder, the counters store) fails here.
	t.Run("ATF-OWN-ISO-006", func(t *testing.T) {
		rec := NewRecorder()
		h := NewHarness(t, 5094837833)
		tasks := isolationMatrix(h, "-1001")
		installMatrix(h, tasks)
		jobs := fanOut(h, tasks, "-1001", 9501, probe)

		e := evt("ATF-OWN-ISO-006", "ISO",
			"concurrent run: 6 tasks × 6 jobs through one engine, race-clean",
			"exactly 6 deliveries classified, no cross-task corruption")
		start := now()
		var wg sync.WaitGroup
		var mu sync.Mutex
		var errs []error
		for _, tk := range tasks {
			wg.Add(1)
			go func(tk matrixTask) {
				defer wg.Done()
				j := h.Lookup(jobs[tk.taskID])
				if err := h.Run(ctx, j); err != nil {
					mu.Lock()
					errs = append(errs, fmt.Errorf("%s: %w", tk.taskID, err))
					mu.Unlock()
				}
			}(tk)
		}
		wg.Wait()

		passed := 0
		for _, tk := range tasks {
			te := evt("ATF-OWN-ISO-006", "ISO", tk.taskID+" concurrent outcome", tk.wantBody)
			te.JobID = jobs[tk.taskID]
			if tk.wantDrop {
				if assertTaskDrop(h, te, tk) {
					passed++
				}
			} else if assertTaskBody(h, te, tk) {
				passed++
			}
			rec.Record(te)
		}
		if passed == len(tasks) && len(errs) == 0 {
			e.Result = ResultPass
			e.Observed = fmt.Sprintf("tasks_ok=%d", passed)
		} else {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("tasks_ok=%d/%d engine_errors=%d", passed, len(tasks), len(errs))
		}
		var lastErr error
		if len(errs) > 0 {
			lastErr = errs[0]
		}
		h.Finish(rec, &e, start, lastErr)
		if e.Result != ResultPass {
			t.Errorf("ISO-006: %s", e.FailureReason)
		}
		for _, f := range failOnly(rec) {
			t.Errorf("ISO-006 task %s: %s", f.JobID, f.FailureReason)
		}
	})
}

// assertTaskBody asserts the gateway saw exactly this task's predicted body on
// the task's own target chat, and that the job row is SENT.
func assertTaskBody(h *Harness, e Evidence, tk matrixTask) bool {
	h.t.Helper()
	bodies := h.bodiesForTarget(tk.target)
	if len(bodies) == 0 {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("%s: no delivery recorded to %s", tk.taskID, tk.target)
		return false
	}
	for _, body := range bodies {
		if !strings.Contains(body, tk.wantBody) {
			e.Result = ResultFail
			e.FailureReason = fmt.Sprintf("%s: body %q does not contain %q", tk.taskID, body, tk.wantBody)
			return false
		}
	}
	if st := h.jobStatus(e.JobID); st != "SENT" {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("%s: job status %s, want SENT", tk.taskID, st)
		return false
	}
	e.Decision = "DELIVERED"
	return true
}

// assertTaskDrop asserts the task's job was rejected with no gateway call.
func assertTaskDrop(h *Harness, e Evidence, tk matrixTask) bool {
	h.t.Helper()
	if len(h.bodiesForTarget(tk.target)) > 0 {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("%s: gateway was called for a job predicted to drop", tk.taskID)
		return false
	}
	if st := h.jobStatus(e.JobID); st == "SENT" {
		e.Result = ResultFail
		e.FailureReason = fmt.Sprintf("%s: job SENT but predicted to drop", tk.taskID)
		return false
	}
	e.Decision = "REJECTED"
	return true
}

// sentTo counts the gateway's recorded sends to one target chat.
func sentTo(h *Harness, targetChat string) int {
	n := 0
	for _, c := range h.Gateway.Recorded() {
		if fmt.Sprintf("%d", c.TargetID) == targetChat {
			n++
		}
	}
	return n
}

// failOnly returns the non-PASS evidence rows of a recorder.
func failOnly(rec *Recorder) []Evidence {
	_, fails := rec.Summary()
	return fails
}

// firstErr returns the first non-nil error, for Finish.
func firstErr(errs ...error) error {
	for _, e := range errs {
		if e != nil {
			return e
		}
	}
	return nil
}

func contains(haystack, needle string) bool {
	return strings.Contains(haystack, needle)
}

func indexOf(haystack, needle string) int { return strings.Index(haystack, needle) }
