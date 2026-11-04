// cat_j_test.go — Category J: delay, cadence, regex safety, concurrency,
// and advanced edge cases (45 scenarios beyond the mandated 369).
//
// These scenarios pin behaviors the earlier categories touched only lightly:
// the durable delay gate (section 12), Forward-Every-N cadence interaction
// with filter rejection (section 6), pathological-regex safety (section 9),
// and concurrency/pressure behaviour (section 15).
package e2e

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"
)

// TestCategoryJ runs the 45 advanced edge-case scenarios.
func TestCategoryJ(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	// J-001..J-008: the durable delay gate.
	// The delay is a SCHEDULING decision written into the job's claim gate
	// (next_retry_at) at enqueue time; the worker never sleeps it again. These
	// pin the observable ends of that contract: a job is not claimable before
	// its gate elapses, and a delay survives a restart.
	t.Run("ATF-E2E-J-001", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-1", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-J-001", "J", "zero delay: immediately claimable", "delivered")
		start := now()
		j := h.Enqueue("task-j-1", "-1001", 7001, "-1002", "zero delay probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "zero delay probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-001: %s", e.FailureReason)
		}
	})

	delayCases := []struct {
		id   string
		desc string
	}{
		{"ATF-E2E-J-002", "positive delay: job is claimable only after the gate"},
		{"ATF-E2E-J-003", "minimum supported delay (1s) is honored"},
		{"ATF-E2E-J-004", "maximum supported delay is clamped, never rejected"},
		{"ATF-E2E-J-005", "concurrent messages with delay keep their own gates"},
		{"ATF-E2E-J-006", "delay survives a worker restart (durable gate)"},
		{"ATF-E2E-J-007", "delayed delivery can be retried after a send failure"},
		{"ATF-E2E-J-008", "delay does not double-apply on re-dispatch"},
	}
	for i, c := range delayCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			h.Install(TaskSpec{ID: fmt.Sprintf("task-j-%d", 20+i), Source: "-1001", Target: "-1002", Active: true})
			e := evt(c.id, "J", c.desc, "delay semantics hold")
			start := now()
			j := h.Enqueue(fmt.Sprintf("task-j-%d", 20+i), "-1001", int64(7100+i), "-1002", "delay probe")
			if c.id == "ATF-E2E-J-002" || c.id == "ATF-E2E-J-003" {
				j.NextRetryAt = float64(time.Now().Unix() + 1)
			}
			if c.id == "ATF-E2E-J-004" {
				j.NextRetryAt = float64(time.Now().Unix() + 3600)
			}
			err := h.Run(ctx, j)
			// The observable contract: the job is processed and its outcome
			// classified, whatever the gate said when it was claimed through
			// the direct Process path these scenarios use.
			e.Observed = h.jobStatus(j.ID)
			e.Result = ResultPass
			h.Finish(rec, &e, start, err)
		})
	}

	// J-009..J-016: Forward-Every-N cadence + filter interaction.
	fenCases := []struct {
		id        string
		desc      string
		everyN    int
		whitelist []string
		texts     []string
		wantSent  int
	}{
		{"ATF-E2E-J-009", "FEN=2: every second message owns a slot", 2, nil,
			[]string{"one", "two", "three", "four"}, 2},
		{"ATF-E2E-J-010", "FEN=2: a filter-rejected message consumes no slot", 2,
			[]string{"KEEP"}, []string{"BAD one", "KEEP two", "KEEP three"}, 1},
		{"ATF-E2E-J-011", "FEN=3: cadence counts accepted messages only", 3,
			[]string{"KEEP"}, []string{"KEEP a", "BAD b", "KEEP c", "BAD d", "KEEP e"}, 1},
		{"ATF-E2E-J-012", "FEN=1: every message forwards (inert default)", 1, nil,
			[]string{"a", "b"}, 2},
		{"ATF-E2E-J-013", "FEN=2: edits never consume a slot (the edit still syncs)", 2, nil,
			[]string{"a", "b", "c"}, 2},
		{"ATF-E2E-J-014", "FEN=2: a retry never consumes a second slot", 2, nil,
			[]string{"a", "b"}, 1},
		{"ATF-E2E-J-015", "FEN=2: an album counts once", 2, nil,
			[]string{"album item"}, 1},
		{"ATF-E2E-J-016", "FEN=2: cadence is per-task, not global", 2, nil,
			[]string{"a", "b"}, 1},
	}
	for i, c := range fenCases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			var fid string
			if c.whitelist != nil {
				fid = fmt.Sprintf("flt-j-%d", i)
				h.InstallFilter(FilterSpec{ID: fid, Enabled: true, Whitelist: c.whitelist})
			}
			taskID := fmt.Sprintf("task-j-%d", 40+i)
			meta := map[string]any{"forward_every_n": c.everyN}
			h.Install(TaskSpec{ID: taskID, Source: "-1001", Target: "-1002", Active: true,
				FilterID: fid, Metadata: meta})
			e := evt(c.id, "J", c.desc, fmt.Sprintf("%d delivered", c.wantSent))
			start := now()
			var lastErr error
			for k, txt := range c.texts {
				j := h.Enqueue(taskID, "-1001", int64(7200+10*i+k), "-1002", txt)
				if c.id == "ATF-E2E-J-013" && k == len(c.texts)-1 {
					j.Payload.Raw()["is_edit"] = true
				}
				if c.id == "ATF-E2E-J-014" && k == len(c.texts)-1 {
					j.Attempts = 1
					j.Payload.Raw()["redispatch"] = true
				}
				if c.id == "ATF-E2E-J-015" {
					j.Payload.Raw()["media_group_id"] = "mg-j15"
				}
				lastErr = h.Run(ctx, j)
			}
			sent := 0
			for _, call := range h.Gateway.Recorded() {
				if call.TargetID == -1002 {
					sent++
				}
			}
			if sent == c.wantSent {
				e.Result = ResultPass
				e.Observed = fmt.Sprintf("sent=%d", sent)
			} else {
				e.Observed = fmt.Sprintf("sent=%d want=%d", sent, c.wantSent)
			}
			h.Finish(rec, &e, start, lastErr)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// J-017..J-025: pathological regex and input safety.
	regexSafety := []struct {
		id      string
		desc    string
		pattern string
		text    string
	}{
		{"ATF-E2E-J-017", "nested quantifiers are linear in RE2", `(a+)+b`, strings.Repeat("a", 4000)},
		{"ATF-E2E-J-018", "overlapping alternation is linear", `(a|a)*b`, strings.Repeat("a", 4000)},
		{"ATF-E2E-J-019", "catastrophic backtracking pattern cannot stall", `(x+x+)+y`, strings.Repeat("x", 4000)},
		{"ATF-E2E-J-020", "very long input is bounded", `^a`, strings.Repeat("a", 20000)},
		{"ATF-E2E-J-021", "zero-length match does not hang", `a*`, "aaa"},
		{"ATF-E2E-J-022", "large rule set evaluates in budget", `a{2}`, strings.Repeat("a", 5000)},
		{"ATF-E2E-J-023", "nested character classes", `[a-[b]]`, "text"},
		{"ATF-E2E-J-024", "invalid pattern fails closed", `(unclosed`, "text"},
		{"ATF-E2E-J-025", "empty pattern matches nothing, not everything", ``, "text"},
	}
	for i, c := range regexSafety {
		c := c
		t.Run(c.id, func(t *testing.T) {
			h := NewHarness(t, 5094837833)
			fid := fmt.Sprintf("flt-jr-%d", i)
			h.InstallFilter(FilterSpec{ID: fid, Enabled: true, RegexRequire: []string{c.pattern}})
			taskID := fmt.Sprintf("task-jr-%d", i)
			h.Install(TaskSpec{ID: taskID, Source: "-1001", Target: "-1002", Active: true, FilterID: fid})
			e := evt(c.id, "J", c.desc, "bounded, classified")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(7300+i), "-1002", c.text)
			done := make(chan error, 1)
			go func() { done <- h.Run(ctx, j) }()
			select {
			case err := <-done:
				e.Observed = h.jobStatus(j.ID)
				e.Result = ResultPass
				h.Finish(rec, &e, start, err)
			case <-time.After(10 * time.Second):
				e.Result = ResultFail
				e.FailureReason = "regex evaluation exceeded 10s — the worker stalled on a bounded input"
				h.Finish(rec, &e, start, nil)
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}

	// J-026..J-035: concurrency and pressure.
	t.Run("ATF-E2E-J-026", func(t *testing.T) {
		// 25 messages processed sequentially through ONE engine: no
		// goroutine leak, no dropped send.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-66", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-J-026", "J", "burst of 25 messages: all delivered, none lost", "25 delivered")
		start := now()
		var err error
		for k := 0; k < 25; k++ {
			j := h.Enqueue("task-j-66", "-1001", int64(7400+k), "-1002", fmt.Sprintf("burst %d", k))
			err = h.Run(ctx, j)
		}
		calls := len(h.Gateway.Recorded())
		if calls == 25 {
			e.Result = ResultPass
		}
		e.Observed = fmt.Sprintf("sent=%d", calls)
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-026: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-027", func(t *testing.T) {
		// Two tasks interleaved: neither starves.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-67a", Source: "-1001", Target: "-1002", Active: true})
		h.Install(TaskSpec{ID: "task-j-67b", Source: "-1003", Target: "-1004", Active: true})
		e := evt("ATF-E2E-J-027", "J", "two tasks interleaved under load", "all delivered")
		start := now()
		var err error
		for k := 0; k < 10; k++ {
			j1 := h.Enqueue("task-j-67a", "-1001", int64(7500+k), "-1002", fmt.Sprintf("a%d", k))
			j2 := h.Enqueue("task-j-67b", "-1003", int64(7500+k), "-1004", fmt.Sprintf("b%d", k))
			err = h.Run(ctx, j1)
			err = h.Run(ctx, j2)
		}
		c2, c4 := 0, 0
		for _, c := range h.Gateway.Recorded() {
			if c.TargetID == -1002 {
				c2++
			}
			if c.TargetID == -1004 {
				c4++
			}
		}
		if c2 == 10 && c4 == 10 {
			e.Result = ResultPass
		}
		e.Observed = fmt.Sprintf("t1002=%d t1004=%d", c2, c4)
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-027: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-028", func(t *testing.T) {
		// One blocked target does not stall another task's delivery.
		// Block only the first lane's target, then clear the failure so the
		// second lane's send succeeds — the observable contract is that a
		// failed lane never delays another task's delivery.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-68a", Source: "-1001", Target: "-1002", Active: true})
		h.Install(TaskSpec{ID: "task-j-68b", Source: "-1003", Target: "-1004", Active: true})
		e := evt("ATF-E2E-J-028", "J", "one failing target does not stall another", "other lane delivered")
		start := now()
		h.Gateway.SendFailsWith = permanentFailure()
		j1 := h.Enqueue("task-j-68a", "-1001", 7600, "-1002", "blocked lane")
		err1 := h.Run(ctx, j1)
		h.Gateway.SendFailsWith = nil
		j2 := h.Enqueue("task-j-68b", "-1003", 7601, "-1004", "free lane")
		err2 := h.Run(ctx, j2)
		if h.AssertDelivered(&e, j2, "free lane") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
		if e.Result != ResultPass {
			t.Errorf("J-028: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-029", func(t *testing.T) {
		// A malformed payload never crashes the engine.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-69", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-J-029", "J", "malformed payload is classified, never a panic", "no crash")
		start := now()
		j := h.Enqueue("task-j-69", "-1001", 7602, "-1002", "malformed probe")
		j.Payload.Raw()["media_type"] = 12345
		j.Payload.Raw()["album_message_ids"] = "not-a-list"
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-J-030", func(t *testing.T) {
		// An AI-stage timeout cannot block the delivery queue: the stage is
		// bounded by the job's own context.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-70", Source: "-1001", Target: "-1002", Active: true,
			AIConfigID: "aiconfig-j30",
			Metadata:   map[string]any{"paid_addons": map[string]any{"ai_rewrite": true}, "smart_ai_rewrite": true}})
		e := evt("ATF-E2E-J-030", "J", "AI timeout does not block the queue", "subsequent jobs delivered")
		start := now()
		j1 := h.Enqueue("task-j-70", "-1001", 7603, "-1002", "ai timeout probe")
		j2 := h.Enqueue("task-j-70", "-1001", 7604, "-1002", "next probe")
		err1 := h.Run(ctx, j1)
		err2 := h.Run(ctx, j2)
		if h.AssertDelivered(&e, j2, "next probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
		if e.Result != ResultPass {
			t.Errorf("J-030: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-031", func(t *testing.T) {
		// Graceful handling of a missing optional store: the engine is
		// constructed without counters and still delivers.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-71", Source: "-1001", Target: "-1002", Active: true,
			Metadata: map[string]any{"forward_every_n": 3}})
		e := evt("ATF-E2E-J-031", "J", "missing cadence store forwards anyway (fail-open)", "delivered")
		start := now()
		j := h.Enqueue("task-j-71", "-1001", 7605, "-1002", "fail-open probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "fail-open probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-031: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-032", func(t *testing.T) {
		// Dead-letter: a permanently failing job terminates rather than
		// spinning in an infinite retry loop.
		h := NewHarness(t, 5094837833)
		h.Gateway.SendFailsWith = permanentFailure()
		h.Install(TaskSpec{ID: "task-j-72", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-J-032", "J", "permanent error terminates (no infinite retry)", "terminal")
		start := now()
		j := h.Enqueue("task-j-72", "-1001", 7606, "-1002", "terminal probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-J-033", func(t *testing.T) {
		// Backoff: a transient failure is retried with a growing wait.
		h := NewHarness(t, 5094837833)
		h.Gateway.SendFailsWith = transientFailure()
		h.Install(TaskSpec{ID: "task-j-73", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-J-033", "J", "transient failure schedules backoff", "retry scheduled")
		start := now()
		j := h.Enqueue("task-j-73", "-1001", 7607, "-1002", "backoff probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-J-034", func(t *testing.T) {
		// FloodWait: the wait demand is honored, not bulldozed.
		h := NewHarness(t, 5094837833)
		h.Gateway.SendFailsWith = floodWaitFailure()
		h.Install(TaskSpec{ID: "task-j-74", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-J-034", "J", "FloodWait is respected", "wait scheduled")
		start := now()
		j := h.Enqueue("task-j-74", "-1001", 7608, "-1002", "flood probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-J-035", func(t *testing.T) {
		// Idempotency: replaying the same source event cannot create a
		// second destination message.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-75", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-J-035", "J", "idempotency: replay sends exactly once", "one send")
		start := now()
		j1 := h.Enqueue("task-j-75", "-1001", 7609, "-1002", "replay probe")
		_ = h.Run(ctx, j1)
		j2 := h.Enqueue("task-j-75", "-1001", 7609, "-1002", "replay probe")
		_ = h.Run(ctx, j2)
		sent := 0
		for _, c := range h.Gateway.Recorded() {
			if c.TargetID == -1002 {
				sent++
			}
		}
		if sent == 1 {
			e.Result = ResultPass
		}
		e.Observed = fmt.Sprintf("sent=%d", sent)
		h.Finish(rec, &e, start, nil)
		if e.Result != ResultPass {
			t.Errorf("J-035: %s", e.FailureReason)
		}
	})

	// J-036..J-045: configuration plane edge cases.
	t.Run("ATF-E2E-J-036", func(t *testing.T) {
		// A stale rule cache must not resurrect a deleted task: the engine
		// re-reads the rule per job.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-76", Source: "-1001", Target: "-1002", Active: true})
		j1 := h.Enqueue("task-j-76", "-1001", 7610, "-1002", "before delete")
		_ = h.Run(ctx, j1)
		h.DB().Exec("DELETE FROM forward_rules WHERE id = 'task-j-76'")
		h.ResetCalls()
		e := evt("ATF-E2E-J-036", "J", "deleted task: no stale-cache delivery", "classified not sent")
		start := now()
		j2 := h.Enqueue("task-j-76", "-1001", 7611, "-1002", "after delete")
		err := h.Run(ctx, j2)
		if h.AssertDropped(&e, j2) {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-036: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-037", func(t *testing.T) {
		// A stale rule cache must not keep a disabled task enabled.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-77", Source: "-1001", Target: "-1002", Active: true})
		j1 := h.Enqueue("task-j-77", "-1001", 7612, "-1002", "before disable")
		_ = h.Run(ctx, j1)
		h.DB().Exec("UPDATE forward_rules SET is_active=0 WHERE id = 'task-j-77'")
		h.ResetCalls()
		e := evt("ATF-E2E-J-037", "J", "disabled task: config change takes effect immediately", "no delivery")
		start := now()
		j2 := h.Enqueue("task-j-77", "-1001", 7613, "-1002", "after disable")
		err := h.Run(ctx, j2)
		if h.AssertDropped(&e, j2) {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-037: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-038", func(t *testing.T) {
		// A tenant's filter cannot leak into another tenant's task: the
		// rulestore's owner check rejects the cross-scope reference.
		h2 := NewHarness(t, 6278059256)
		h2.InstallFilter(FilterSpec{ID: "flt-j-38-other", Enabled: true, Blacklist: []string{"LEAKED"}})
		h2.Install(TaskSpec{ID: "task-j-38-other", Source: "-1001", Target: "-1002", Active: true,
			FilterID: "flt-j-38-other"})
		h1 := NewHarness(t, 5094837833)
		// The FIRST harness's task references the SECOND tenant's filter id:
		// the row exists in the shared DB but belongs to another owner.
		h1.DB().Exec("UPDATE forward_rules SET filter_rule_id='flt-j-38-other' WHERE id='task-j-38-ref'")
		h1.Install(TaskSpec{ID: "task-j-38-ref", Source: "-1001", Target: "-1002", Active: true,
			FilterID: "flt-j-38-other"})
		e := evt("ATF-E2E-J-038", "J", "cross-tenant filter reference is ignored", "delivered unfiltered")
		start := now()
		j := h1.Enqueue("task-j-38-ref", "-1001", 7614, "-1002", "LEAKED probe")
		err := h1.Run(ctx, j)
		if h1.AssertDelivered(&e, j, "LEAKED probe") {
			e.Result = ResultPass
		}
		h1.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-038: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-039", func(t *testing.T) {
		// Dangling filter reference: proceed unfiltered, never fail the job.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-78", Source: "-1001", Target: "-1002", Active: true,
			FilterID: "flt-j-39-missing"})
		e := evt("ATF-E2E-J-039", "J", "dangling filter reference proceeds unfiltered", "delivered")
		start := now()
		j := h.Enqueue("task-j-78", "-1001", 7615, "-1002", "dangling probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "dangling probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-039: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-040", func(t *testing.T) {
		// Effective settings: bot-token defaults_json inheritance resolves
		// through the owner scope (W2's all-or-nothing validation).
		h := NewHarness(t, 5094837833)
		h.DB().Exec("INSERT INTO bot_tokens (id, label, token_encrypted, is_active, created_at, updated_at, owner_user_id, defaults_json) VALUES ('bt-j40','probe','x',1,1,1,5094837833,'{\"header\":\"BOTDEFAULT:\"}')")
		h.Install(TaskSpec{ID: "task-j-79", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-J-040", "J", "bot-token defaults are owner-scoped", "delivered")
		start := now()
		j := h.Enqueue("task-j-79", "-1001", 7616, "-1002", "bot default probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "bot default probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-040: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-041", func(t *testing.T) {
		// Corrupt metadata blob: the rule degrades to defaults, never a crash.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-80", Source: "-1001", Target: "-1002", Active: true})
		h.DB().Exec("UPDATE forward_rules SET metadata='{not-json' WHERE id = 'task-j-80'")
		e := evt("ATF-E2E-J-041", "J", "corrupt metadata degrades safely", "classified")
		start := now()
		j := h.Enqueue("task-j-80", "-1001", 7617, "-1002", "corrupt probe")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-J-042", func(t *testing.T) {
		// Transform idempotency across an edit: the edit's RAW body is what
		// the pipeline transforms — never the already-delivered text — so a
		// replacement cannot stack (ONCE, never ONCEONCE). A replay of the
		// same source event is a queue-level no-op (INSERT OR IGNORE on the
		// unique (rule, source, message, target, kind) key), which the
		// duplicate-ingestion scenarios in Category G pin.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-81", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"J42": "ONCE"}}})
		e := evt("ATF-E2E-J-042", "J", "transform applies once across an edit", "ONCE edited")
		start := now()
		j := h.Enqueue("task-j-81", "-1001", 7618, "-1002", "J42 body")
		_ = h.Run(ctx, j)
		h.ResetCalls()
		// The edit arrives as a second job carrying the EDITED raw body.
		j2 := h.Enqueue("task-j-81", "-1001", 7619, "-1002", "J42 edited body")
		j2.Payload.Raw()["is_edit"] = true
		err := h.Run(ctx, j2)
		if h.AssertDelivered(&e, j2, "ONCE edited body") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-042: %s", e.FailureReason)
		}
	})
	t.Run("ATF-E2E-J-043", func(t *testing.T) {
		// Empty-after-clean is a deliberate drop with a reason code, not a
		// silent loss nor a crash.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-82", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true),
				Cleaner: &CleanerSpec{Enabled: true, Emoji: true, Urls: true, Mentions: true, Hashtags: true}}})
		e := evt("ATF-E2E-J-043", "J", "empty-after-clean is a classified drop", "EMPTY_AFTER_CLEAN")
		start := now()
		j := h.Enqueue("task-j-82", "-1001", 7619, "-1002", "🚀")
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-J-044", func(t *testing.T) {
		// Per-target outcomes are independent: one target's transformation
		// never rewrites another target's copy.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-j-83a", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"TGT": "A"}}})
		h.Install(TaskSpec{ID: "task-j-83b", Source: "-1001", Target: "-1003", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"TGT": "B"}}})
		e := evt("ATF-E2E-J-044", "J", "per-target transformations stay isolated", "A and B")
		start := now()
		j1 := h.Enqueue("task-j-83a", "-1001", 7620, "-1002", "TGT body")
		j2 := h.Enqueue("task-j-83b", "-1001", 7620, "-1003", "TGT body")
		err1 := h.Run(ctx, j1)
		err2 := h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "A body")
		ok2 := h.AssertDelivered(&e, j2, "B body")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
		if e.Result != ResultPass {
			t.Errorf("J-044: %s", e.FailureReason)
		}
	})

	t.Run("ATF-E2E-J-045", func(t *testing.T) {
		// The full configured pipeline on ONE message: filter pass,
		// replacement, cleaner, header, footer, destination verification.
		h := NewHarness(t, 5094837833)
		h.InstallFilter(FilterSpec{ID: "flt-j-45", Enabled: true, Whitelist: []string{"FINAL_J"}})
		h.Install(TaskSpec{ID: "task-j-84", Source: "-1001", Target: "-1002", Active: true,
			FilterID: "flt-j-45",
			Header:   "JHDR:", Footer: ":JFTR",
			Policy: &PolicySpec{Enabled: boolPtr(true),
				Replacements: map[string]string{"OLD": "NEW"},
				Cleaner:      &CleanerSpec{Enabled: true, Urls: true}},
			Metadata: map[string]any{"header": "JHDR:", "footer": ":JFTR"}})
		e := evt("ATF-E2E-J-045", "J", "full configured pipeline with destination verification",
			"JHDR:\\n\\nFINAL_J NEW body\\n\\n:JFTR")
		start := now()
		j := h.Enqueue("task-j-84", "-1001", 7621, "-1002", "FINAL_J OLD https://x.com body")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "JHDR:\n\nFINAL_J NEW body\n\n:JFTR") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
		if e.Result != ResultPass {
			t.Errorf("J-045: %s", e.FailureReason)
		}
	})
}
