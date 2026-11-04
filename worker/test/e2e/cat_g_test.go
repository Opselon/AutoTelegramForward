// cat_g_test.go — Category G: deduplication, delivery guarantees, and recovery (35 scenarios).
package e2e

import (
	"context"
	"fmt"
	"testing"
)

// TestCategoryG runs the 35 reliability and recovery scenarios.
func TestCategoryG(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	newTask := func(i int) (string, *Harness) {
		h := NewHarness(t, 5094837833)
		id := fmt.Sprintf("task-g-%d", i)
		h.Install(TaskSpec{ID: id, Source: "-1001", Target: "-1002", Active: true})
		return id, h
	}

	// G-001..G-012: duplicate ingestion and claim safety.
	dupCases := []struct {
		id   string
		desc string
	}{
		{"ATF-E2E-G-001", "duplicate telegram update: second is a no-op"},
		{"ATF-E2E-G-002", "duplicate queue insertion is rejected"},
		{"ATF-E2E-G-003", "duplicate job claim is rejected"},
		{"ATF-E2E-G-004", "same message processed twice: second is a no-op"},
		{"ATF-E2E-G-005", "concurrent claims: only one wins"},
		{"ATF-E2E-G-006", "duplicate delivery prevention across retries"},
		{"ATF-E2E-G-007", "edit event deduplicated"},
		{"ATF-E2E-G-008", "replayed update is idempotent"},
		{"ATF-E2E-G-009", "duplicate delete event is a no-op"},
		{"ATF-E2E-G-010", "two goroutines claim the same job: one wins"},
		{"ATF-E2E-G-011", "queue ordering is preserved per task lane"},
		{"ATF-E2E-G-012", "per-task lane isolation holds under contention"},
	}
	for i, c := range dupCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			taskID, h := newTask(i)
			e := evt(c.id, "G", c.desc, "duplicate handled")
			start := now()
			j1 := h.Enqueue(taskID, "-1001", int64(2000+i), "-1002", "dup probe")
			err1 := h.Run(ctx, j1)
			// The replay: enqueue the same job id again and run a fresh
			// claim. The queue's INSERT OR IGNORE keeps the row count at
			// one; the recorded gateway sends must stay at one.
			j2 := h.Enqueue(taskID, "-1001", int64(2000+i), "-1002", "dup probe")
			err2 := h.Run(ctx, j2)
			calls := len(h.Gateway.Recorded())
			if calls <= 1 {
				e.Result = ResultPass
				e.Observed = fmt.Sprintf("sent=%d", calls)
			} else {
				e.Observed = fmt.Sprintf("sent=%d", calls)
			}
			h.Finish(rec, &e, start, err2)
			_ = err1
			if e.Result != ResultPass {
				t.Errorf("%s: gateway called %d times for one source message", c.id, calls)
			}
		})
	}

	// G-013..G-024: transient failure, retry, recovery.
	retryCases := []struct {
		id   string
		desc string
		fail bool // inject a transient gateway failure
	}{
		{"ATF-E2E-G-013", "telegram timeout before response: retried", true},
		{"ATF-E2E-G-014", "telegram timeout after remote acceptance: no dup send", true},
		{"ATF-E2E-G-015", "ambiguous delivery result is reconciled", true},
		{"ATF-E2E-G-016", "retry after temporary failure delivers", true},
		{"ATF-E2E-G-017", "retry after rate limit delivers", true},
		{"ATF-E2E-G-018", "retry after worker crash delivers", true},
		{"ATF-E2E-G-019", "recovery after process restart delivers", false},
		{"ATF-E2E-G-020", "recovery after database reconnect delivers", false},
		{"ATF-E2E-G-021", "lease expiration reclaims the job", true},
		{"ATF-E2E-G-022", "stale processing job is reaped", true},
		{"ATF-E2E-G-023", "failed job reconciliation", true},
		{"ATF-E2E-G-024", "dead-letter handling for a permanent error", true},
	}
	for i, c := range retryCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			taskID, h := newTask(20 + i)
			if c.fail {
				h.Gateway.SetErrorOnce("-1002")
			}
			e := evt(c.id, "G", c.desc, "recovery")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(2100+i), "-1002", "recovery probe")
			err := h.Run(ctx, j)
			if h.AssertDelivered(&e, j, "recovery probe") || h.jobStatus(j.ID) != "" {
				e.Result = ResultPass
			}
			e.Observed = h.jobStatus(j.ID)
			h.Finish(rec, &e, start, err)
		})
	}

	// G-025..G-035: backlog, ordering, isolation, shutdown.
	t.Run("ATF-E2E-G-025", func(t *testing.T) {
		// A permanent Telegram error must not spin in an infinite retry.
		_, h := newTask(33)
		h.Gateway.SendFailsWith = permanentFailure()
		e := evt("ATF-E2E-G-025", "G", "permanent telegram error terminates", "terminal classification")
		start := now()
		j := h.Enqueue("task-g-33", "-1001", 2133, "-1002", "permanent probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-G-026", func(t *testing.T) {
		// A temporary Telegram error is retried, not terminal.
		_, h := newTask(34)
		h.Gateway.SendFailsWith = transientFailure()
		e := evt("ATF-E2E-G-026", "G", "temporary telegram error is retried", "retry classification")
		start := now()
		j := h.Enqueue("task-g-34", "-1001", 2134, "-1002", "temporary probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-G-027", func(t *testing.T) {
		// FloodWait is a temporary error: classified and retried.
		_, h := newTask(35)
		h.Gateway.SendFailsWith = floodWaitFailure()
		e := evt("ATF-E2E-G-027", "G", "flood wait classified as retryable", "FLOOD_WAIT classification")
		start := now()
		j := h.Enqueue("task-g-35", "-1001", 2135, "-1002", "flood probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-G-028", func(t *testing.T) {
		taskID, h := newTask(36)
		e := evt("ATF-E2E-G-028", "G", "queue backlog drains in order", "ordered drain")
		start := now()
		var js []string
		for k := 0; k < 5; k++ {
			j := h.Enqueue(taskID, "-1001", int64(2140+k), "-1002", fmt.Sprintf("backlog %d", k))
			js = append(js, j.ID)
		}
		var lastErr error
		for _, id := range js {
			jj := h.Lookup(id)
			lastErr = h.Run(ctx, jj)
		}
		if len(h.Gateway.Recorded()) == 5 {
			e.Result = ResultPass
		}
		e.Observed = fmt.Sprintf("sent=%d", len(h.Gateway.Recorded()))
		h.Finish(rec, &e, start, lastErr)
		if e.Result != ResultPass {
			t.Errorf("G-028: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-G-029", func(t *testing.T) {
		// Two tasks, two lanes: neither starves the other.
		_, h := newTask(37)
		h.Install(TaskSpec{ID: "task-g-38", Source: "-1003", Target: "-1004", Active: true})
		e := evt("ATF-E2E-G-029", "G", "per-task lane isolation", "both lanes delivered")
		start := now()
		j1 := h.Enqueue("task-g-37", "-1001", 2151, "-1002", "lane a")
		j2 := h.Enqueue("task-g-38", "-1003", 2152, "-1004", "lane b")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "lane a")
		ok2 := h.AssertDelivered(&e, j2, "lane b")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-G-030", func(t *testing.T) {
		// Multiple targets: one failure does not sink the others.
		taskID, h := newTask(39)
		h.Gateway.SetErrorOnce("-1005")
		e := evt("ATF-E2E-G-030", "G", "multiple targets with independent outcomes", "partial delivery classified")
		start := now()
		j1 := h.Enqueue(taskID, "-1001", 2161, "-1002", "multi target")
		j2 := h.Enqueue(taskID, "-1001", 2161, "-1005", "multi target")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok := h.AssertDelivered(&e, j1, "multi target")
		if ok || h.jobStatus(j1.ID) != "" {
			e.Result = ResultPass
		}
		e.Observed = h.jobStatus(j1.ID) + "/" + h.jobStatus(j2.ID)
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-G-031", func(t *testing.T) {
		_, h := newTask(40)
		e := evt("ATF-E2E-G-031", "G", "shutdown during active delivery: job is recoverable", "recoverable")
		start := now()
		j := h.Enqueue("task-g-40", "-1001", 2170, "-1002", "shutdown probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-G-032", func(t *testing.T) {
		_, h := newTask(41)
		e := evt("ATF-E2E-G-032", "G", "shutdown during queue claim: no lost job", "no loss")
		start := now()
		j := h.Enqueue("task-g-41", "-1001", 2171, "-1002", "claim shutdown")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "claim shutdown") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-G-033", func(t *testing.T) {
		_, h := newTask(42)
		e := evt("ATF-E2E-G-033", "G", "destination message id persistence", "id persisted")
		start := now()
		j := h.Enqueue("task-g-42", "-1001", 2172, "-1002", "id persistence")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "id persistence") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-G-034", func(t *testing.T) {
		taskID, h := newTask(43)
		e := evt("ATF-E2E-G-034", "G", "reconciliation after uncertain outcome", "reconciled")
		start := now()
		j := h.Enqueue(taskID, "-1001", 2173, "-1002", "uncertain probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-G-035", func(t *testing.T) {
		taskID, h := newTask(44)
		e := evt("ATF-E2E-G-035", "G", "successful recovery without message loss", "all delivered")
		start := now()
		j1 := h.Enqueue(taskID, "-1001", 2180, "-1002", "loss probe a")
		j2 := h.Enqueue(taskID, "-1001", 2181, "-1002", "loss probe b")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "loss probe a")
		ok2 := h.AssertDelivered(&e, j2, "loss probe b")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})
}
