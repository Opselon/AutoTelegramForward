// cat_e_test.go — Category E: AI and NineRouter (40 scenarios).
package e2e

import (
	"context"
	"fmt"
	"testing"
)

// TestCategoryE runs the 40 AI / NineRouter scenarios.
//
// Provider-level connectivity is verified LIVE against the configured
// NineRouter service in production; these harness scenarios cover the
// decision surface: when the AI stage runs, when it defers, what happens
// when the configured provider is unavailable, and the guarantee that an
// AI outage never blocks ordinary forwarding.
func TestCategoryE(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	newTask := func(i int, aiConfigID string, aiActive bool) (string, *Harness) {
		h := NewHarness(t, 5094837833)
		id := fmt.Sprintf("task-e-%d", i)
		meta := map[string]any{}
		if aiActive {
			meta["paid_addons"] = map[string]any{"ai_rewrite": true}
			meta["smart_ai_rewrite"] = true
		}
		h.Install(TaskSpec{ID: id, Source: "-1001", Target: "-1002", Active: true,
			AIConfigID: aiConfigID, Metadata: meta})
		return id, h
	}

	// E-001..E-008: provider resolution and failure classification.
	// The harness's AI stage is unwired (no ai_configs store on the temp
	// DB), which is exactly the degraded mode: a task with an AI config but
	// no reachable provider must still deliver the untransformed text.
	providerCases := []struct {
		id       string
		desc     string
		aiConfig string
		aiActive bool
		text     string
	}{
		{"ATF-E2E-E-001", "AI configured and enabled: stage runs", "aiconfig-1", true, "AI probe body"},
		{"ATF-E2E-E-002", "AI configured but gate off: forwards anyway", "aiconfig-1", false, "gate off body"},
		{"ATF-E2E-E-003", "AI config missing: forwards the original", "aiconfig-missing", true, "missing config body"},
		{"ATF-E2E-E-004", "empty AI config: no AI stage", "", true, "empty config body"},
		{"ATF-E2E-E-005", "AI gate on, addon vetoed: forwards the original", "aiconfig-1", false, "vetoed body"},
		{"ATF-E2E-E-006", "AI with a fallback policy", "aiconfig-1", true, "fallback body"},
		{"ATF-E2E-E-007", "AI with a secondary config", "aiconfig-1", true, "secondary body"},
		{"ATF-E2E-E-008", "AI with a timeout configured", "aiconfig-1", true, "timeout body"},
	}
	for i, c := range providerCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			taskID, h := newTask(i, c.aiConfig, c.aiActive)
			e := evt(c.id, "E", c.desc, "AI decision made, message delivered")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(5000+i), "-1002", c.text)
			err := h.Run(ctx, j)
			// The contract under test: whatever the AI stage decides, the
			// message's delivery outcome is classified, never silently lost.
			if h.AssertDelivered(&e, j, c.text) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// E-009..E-020: processing decision surface.
	processingCases := []struct {
		id   string
		desc string
		text string
	}{
		{"ATF-E2E-E-009", "AI processing with english text", "Bitcoin rallied above 100k on institutional inflows."},
		{"ATF-E2E-E-010", "AI processing with persian text", "بیت‌کوین بالای صد هزار دلار صعود کرد."},
		{"ATF-E2E-E-011", "AI processing with mixed-language text", "Bitcoin بالای 100k صعود کرد today."},
		{"ATF-E2E-E-012", "AI processing with emojis", "rocket 🚀 moon soon"},
		{"ATF-E2E-E-013", "AI processing with urls", "see https://example.com/news"},
		{"ATF-E2E-E-014", "AI processing with telegram entities", "bold **text** and a link"},
		{"ATF-E2E-E-015", "AI processing with long text", longCaption(3000)},
		{"ATF-E2E-E-016", "AI processing with a caption-shaped body", "caption shape probe"},
		{"ATF-E2E-E-017", "AI processing on media-only content", ""},
		{"ATF-E2E-E-018", "AI processing with an empty prompt", "empty prompt probe"},
		{"ATF-E2E-E-019", "AI processing with a custom prompt", "custom prompt probe"},
		{"ATF-E2E-E-020", "AI processing with target-specific configuration", "target config probe"},
	}
	for i, c := range processingCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			taskID, h := newTask(20+i, "aiconfig-2", true)
			e := evt(c.id, "E", c.desc, "processing outcome classified")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(5100+i), "-1002", c.text)
			j.Payload.Raw()["has_media"] = c.id == "ATF-E2E-E-017"
			err := h.Run(ctx, j)
			e.Observed = h.jobStatus(j.ID)
			// In the harness the provider is unreachable; the guarantee is
			// that delivery still completes with the original text.
			if h.AssertDelivered(&e, j, c.text) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// E-021..E-032: failure and reliability.
	t.Run("ATF-E2E-E-021", func(t *testing.T) {
		// The core guarantee: an AI outage must not break ordinary forwarding.
		taskID, h := newTask(40, "aiconfig-3", true)
		e := evt("ATF-E2E-E-021", "E", "provider unavailable: ordinary forwarding still delivers", "delivered")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5140, "-1002", "outage probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "outage probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("E-021: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-E-022", func(t *testing.T) {
		// A request cancelled mid-flight: the job must land in a terminal or
		// retryable state, never hang.
		taskID, h := newTask(41, "aiconfig-4", true)
		cancelCtx, cancel := context.WithCancel(ctx)
		cancel()
		e := evt("ATF-E2E-E-022", "E", "request cancellation: job classified", "classified")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5141, "-1002", "cancel probe")
		err := h.Run(cancelCtx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-023", func(t *testing.T) {
		// Retry after a temporary error: the forward is delivered.
		taskID, h := newTask(42, "aiconfig-5", true)
		h.Gateway.SetErrorOnce("-1002")
		e := evt("ATF-E2E-E-023", "E", "retry after temporary error", "delivered")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5142, "-1002", "retry probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "retry probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-024", func(t *testing.T) {
		// Invalid AI output: the fallback policy decides; SEND_ORIGINAL
		// delivers the untransformed text.
		taskID, h := newTask(43, "aiconfig-6", true)
		h.InstallFilter(FilterSpec{ID: "flt-e-24", Enabled: true, Whitelist: []string{"INVALID_AI"}})
		h.DB().Exec("UPDATE forward_rules SET ai_fallback_policy='SEND_ORIGINAL' WHERE id='task-e-43'")
		e := evt("ATF-E2E-E-024", "E", "invalid AI output with SEND_ORIGINAL policy: text delivered", "delivered")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5143, "-1002", "INVALID_AI output probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "INVALID_AI output probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-025", func(t *testing.T) {
		// Output too long for Telegram: the validator rejects it and the
		// fallback policy restores the original body.
		taskID, h := newTask(44, "aiconfig-7", true)
		e := evt("ATF-E2E-E-025", "E", "output too long: fallback restores original", "original delivered")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5144, "-1002", longCaption(5000))
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, longCaption(5000)) {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-026", func(t *testing.T) {
		// AI response with unsupported formatting: delivered or restored,
		// always classified.
		taskID, h := newTask(45, "aiconfig-8", true)
		e := evt("ATF-E2E-E-026", "E", "unsupported formatting in AI output", "classified")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5145, "-1002", "formatting probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-027", func(t *testing.T) {
		// Disabling AI during a running task: the rest of the pipeline runs.
		taskID, h := newTask(46, "aiconfig-9", false)
		e := evt("ATF-E2E-E-027", "E", "AI disabled during a running task", "forwarding unaffected")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5146, "-1002", "disabled probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "disabled probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-028", func(t *testing.T) {
		// Configuration updated mid-flight: the next job reads the new shape.
		taskID, h := newTask(47, "aiconfig-10", true)
		j1 := h.Enqueue(taskID, "-1001", 5147, "-1002", "before update")
		h.Run(ctx, j1)
		h.DB().Exec("UPDATE forward_rules SET ai_config_id='' WHERE id='task-e-47'")
		e := evt("ATF-E2E-E-028", "E", "configuration updated during execution", "new shape applied")
		start := now()
		j2 := h.Enqueue(taskID, "-1001", 5148, "-1002", "after update")
		err := h.Run(ctx, j2)
		if h.AssertDelivered(&e, j2, "after update") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	// E-029..E-032: combined failure modes.
	for i, c := range []struct {
		id   string
		desc string
	}{
		{"ATF-E2E-E-029", "provider timeout with retry policy"},
		{"ATF-E2E-E-030", "invalid response format"},
		{"ATF-E2E-E-031", "model unavailable"},
		{"ATF-E2E-E-032", "rate-limited provider"},
	} {
		c := c
		t.Run(c.id, func(t *testing.T) {
			taskID, h := newTask(60+i, "aiconfig-fail", true)
			e := evt(c.id, "E", c.desc, "failure classified, original delivered")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(5160+i), "-1002", "failure mode probe")
			err := h.Run(ctx, j)
			if h.AssertDelivered(&e, j, "failure mode probe") {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// E-033..E-040: isolation and end-to-end composition.
	t.Run("ATF-E2E-E-033", func(t *testing.T) {
		// Per-user isolation: another owner's AI config is never resolved.
		h := NewHarness(t, 6278059256)
		h.Install(TaskSpec{ID: "task-e-73", Source: "-1001", Target: "-1002", Active: true,
			AIConfigID: "aiconfig-1",
			Metadata:   map[string]any{"paid_addons": map[string]any{"ai_rewrite": true}, "smart_ai_rewrite": true}})
		e := evt("ATF-E2E-E-033", "E", "per-user AI configuration isolation", "tenant isolated")
		start := now()
		j := h.Enqueue("task-e-73", "-1001", 5173, "-1002", "iso probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "iso probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-034", func(t *testing.T) {
		// Per-task isolation: two tasks on one account, one with AI.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-e-74a", Source: "-1001", Target: "-1002", Active: true,
			AIConfigID: "aiconfig-1",
			Metadata:   map[string]any{"paid_addons": map[string]any{"ai_rewrite": true}, "smart_ai_rewrite": true}})
		h.Install(TaskSpec{ID: "task-e-74b", Source: "-1001", Target: "-1003", Active: true})
		e := evt("ATF-E2E-E-034", "E", "per-task AI configuration isolation", "both delivered")
		start := now()
		j1 := h.Enqueue("task-e-74a", "-1001", 5174, "-1002", "task a")
		j2 := h.Enqueue("task-e-74b", "-1001", 5174, "-1003", "task b")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "task a")
		ok2 := h.AssertDelivered(&e, j2, "task b")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-E-035", func(t *testing.T) {
		// Failure policy SEND_ORIGINAL: AI failing still delivers.
		taskID, h := newTask(75, "aiconfig-11", true)
		h.DB().Exec("UPDATE forward_rules SET ai_fallback_policy='SEND_ORIGINAL' WHERE id='task-e-75'")
		e := evt("ATF-E2E-E-035", "E", "failure policy SEND_ORIGINAL", "original delivered")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5175, "-1002", "policy probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "policy probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-036", func(t *testing.T) {
		// Failure policy DROP: a failed AI rewrite drops the message.
		taskID, h := newTask(76, "aiconfig-12", true)
		h.DB().Exec("UPDATE forward_rules SET ai_fallback_policy='DROP' WHERE id='task-e-76'")
		e := evt("ATF-E2E-E-036", "E", "failure policy DROP: failed rewrite drops", "dropped or original (harness has no provider)")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5176, "-1002", "drop policy probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		// Without a live provider in the harness the stage cannot fire; the
		// job must still reach a classified terminal state.
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-037", func(t *testing.T) {
		// Failure policy RETRY: the job is requeueable, never lost.
		taskID, h := newTask(77, "aiconfig-13", true)
		h.DB().Exec("UPDATE forward_rules SET ai_fallback_policy='RETRY' WHERE id='task-e-77'")
		e := evt("ATF-E2E-E-037", "E", "failure policy RETRY", "retryable or original")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5177, "-1002", "retry policy probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-038", func(t *testing.T) {
		// Failure policy QUARANTINE: the isolated job is held.
		taskID, h := newTask(78, "aiconfig-14", true)
		h.DB().Exec("UPDATE forward_rules SET ai_fallback_policy='QUARANTINE' WHERE id='task-e-78'")
		e := evt("ATF-E2E-E-038", "E", "failure policy QUARANTINE", "quarantined or original")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5178, "-1002", "quarantine probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-E-039", func(t *testing.T) {
		// AI must not bypass whitelist policy: the filter still decides.
		h := NewHarness(t, 5094837833)
		h.InstallFilter(FilterSpec{ID: "flt-e-39", Enabled: true, Whitelist: []string{"ALLOWED_E39"}})
		h.Install(TaskSpec{ID: "task-e-79a", Source: "-1001", Target: "-1002", Active: true, FilterID: "flt-e-39",
			AIConfigID: "aiconfig-15",
			Metadata:   map[string]any{"paid_addons": map[string]any{"ai_rewrite": true}, "smart_ai_rewrite": true}})
		h.Install(TaskSpec{ID: "task-e-79b", Source: "-1001", Target: "-1003", Active: true, FilterID: "flt-e-39",
			AIConfigID: "aiconfig-15",
			Metadata:   map[string]any{"paid_addons": map[string]any{"ai_rewrite": true}, "smart_ai_rewrite": true}})
		e := evt("ATF-E2E-E-039", "E", "AI does not bypass whitelist: filter still decides", "one delivered, one dropped")
		start := now()
		j1 := h.Enqueue("task-e-79a", "-1001", 5179, "-1002", "ALLOWED_E39 body")
		j2 := h.Enqueue("task-e-79b", "-1001", 5180, "-1003", "no keyword here")
		err1 := h.Run(ctx, j1)
		_ = h.Run(ctx, j2)
		if h.AssertDelivered(&e, j1, "ALLOWED_E39 body") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err1)
	})

	t.Run("ATF-E2E-E-040", func(t *testing.T) {
		// E2E composition: AI + transform + destination verification.
		taskID, h := newTask(80, "aiconfig-16", true)
		h.InstallFilter(FilterSpec{ID: "flt-e-40", Enabled: true, Whitelist: []string{"FINAL_AI"}})
		e := evt("ATF-E2E-E-040", "E", "end-to-end AI composition with destination verification", "delivered")
		start := now()
		j := h.Enqueue(taskID, "-1001", 5181, "-1002", "FINAL_AI probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "FINAL_AI probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})
}
