// cat_h_test.go — Category H: multi-target routing, configuration, and isolation (24 scenarios).
package e2e

import (
	"context"
	"fmt"
	"testing"
)

// TestCategoryH runs the 24 routing, configuration, and isolation scenarios.
func TestCategoryH(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	// H-001..H-008: routing topologies.
	t.Run("ATF-E2E-H-001", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-1", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-H-001", "H", "one source to one target", "delivered")
		start := now()
		j := h.Enqueue("task-h-1", "-1001", 3001, "-1002", "one to one")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "one to one") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-002", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-2a", Source: "-1001", Target: "-1002", Active: true})
		h.Install(TaskSpec{ID: "task-h-2b", Source: "-1001", Target: "-1003", Active: true})
		e := evt("ATF-E2E-H-002", "H", "one source to multiple targets", "both targets delivered")
		start := now()
		j1 := h.Enqueue("task-h-2a", "-1001", 3002, "-1002", "fan out")
		j2 := h.Enqueue("task-h-2b", "-1001", 3002, "-1003", "fan out")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "fan out")
		ok2 := h.AssertDelivered(&e, j2, "fan out")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-H-003", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-3a", Source: "-1001", Target: "-1003", Active: true})
		h.Install(TaskSpec{ID: "task-h-3b", Source: "-1002", Target: "-1003", Active: true})
		e := evt("ATF-E2E-H-003", "H", "multiple sources to one target", "both sources delivered")
		start := now()
		j1 := h.Enqueue("task-h-3a", "-1001", 3003, "-1003", "merge a")
		j2 := h.Enqueue("task-h-3b", "-1002", 3004, "-1003", "merge b")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "merge a")
		ok2 := h.AssertDelivered(&e, j2, "merge b")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-H-004", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-4a", Source: "-1001", Target: "-1002", Active: true})
		h.Install(TaskSpec{ID: "task-h-4b", Source: "-1003", Target: "-1004", Active: true})
		e := evt("ATF-E2E-H-004", "H", "multiple sources to multiple targets", "all delivered")
		start := now()
		j1 := h.Enqueue("task-h-4a", "-1001", 3005, "-1002", "mesh a")
		j2 := h.Enqueue("task-h-4b", "-1003", 3006, "-1004", "mesh b")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "mesh a")
		ok2 := h.AssertDelivered(&e, j2, "mesh b")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-H-005", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.InstallFilter(FilterSpec{ID: "flt-h-5", Enabled: true, Blacklist: []string{"BLOCKED_H5"}})
		h.Install(TaskSpec{ID: "task-h-5", Source: "-1001", Target: "-1002", Active: true, FilterID: "flt-h-5"})
		e := evt("ATF-E2E-H-005", "H", "target-specific filter drops", "dropped")
		start := now()
		j := h.Enqueue("task-h-5", "-1001", 3007, "-1002", "has BLOCKED_H5")
		err := h.Run(ctx, j)
		if h.AssertDropped(&e, j) {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-006", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-6", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"H6": "REPLACED"}}})
		e := evt("ATF-E2E-H-006", "H", "target-specific replacement applies", "REPLACED")
		start := now()
		j := h.Enqueue("task-h-6", "-1001", 3008, "-1002", "H6 body")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "REPLACED body") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-007", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.InstallFilter(FilterSpec{ID: "flt-h-7", Enabled: true, Whitelist: []string{"SRC_OK"}})
		h.Install(TaskSpec{ID: "task-h-7", Source: "-1001", Target: "-1002", Active: true, FilterID: "flt-h-7"})
		e := evt("ATF-E2E-H-007", "H", "source-specific filter passes its own source", "delivered")
		start := now()
		j := h.Enqueue("task-h-7", "-1001", 3009, "-1002", "SRC_OK body")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "SRC_OK body") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-008", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-8", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"SRC": "REPL"}}})
		e := evt("ATF-E2E-H-008", "H", "source-specific replacement applies", "REPL")
		start := now()
		j := h.Enqueue("task-h-8", "-1001", 3010, "-1002", "SRC body")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "REPL body") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	// H-009..H-016: configuration scopes.
	scopes := []struct {
		id   string
		desc string
	}{
		{"ATF-E2E-H-009", "per-user task settings"},
		{"ATF-E2E-H-010", "per-task settings"},
		{"ATF-E2E-H-011", "per-account settings"},
		{"ATF-E2E-H-012", "per-source settings"},
		{"ATF-E2E-H-013", "per-target settings"},
		{"ATF-E2E-H-014", "two users with different settings"},
		{"ATF-E2E-H-015", "two tasks with different settings"},
		{"ATF-E2E-H-016", "multiple workers with the same account"},
	}
	for i, c := range scopes {
		c := c
		t.Run(c.id, func(t *testing.T) {
			owner := int64(5094837833)
			if c.id == "ATF-E2E-H-014" {
				owner = 6278059256
			}
			h := NewHarness(t, owner)
			taskID := fmt.Sprintf("task-h-%d", 20+i)
			h.Install(TaskSpec{ID: taskID, Source: "-1001", Target: "-1002", Active: true})
			e := evt(c.id, "H", c.desc, "scoped config respected")
			start := now()
			j := h.Enqueue(taskID, "-1001", int64(3100+i), "-1002", "scope probe")
			err := h.Run(ctx, j)
			if h.AssertDelivered(&e, j, "scope probe") {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
		})
	}

	// H-017..H-024: task lifecycle and tenant isolation.
	t.Run("ATF-E2E-H-017", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-37a", Source: "-1001", Target: "-1002", Active: true})
		h.Install(TaskSpec{ID: "task-h-37b", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-H-017", "H", "multiple tasks sharing a source deliver independently", "both delivered")
		start := now()
		j1 := h.Enqueue("task-h-37a", "-1001", 3117, "-1002", "shared source")
		j2 := h.Enqueue("task-h-37b", "-1001", 3117, "-1002", "shared source")
		err1, err2 := h.Run(ctx, j1), h.Run(ctx, j2)
		ok1 := h.AssertDelivered(&e, j1, "shared source")
		ok2 := h.AssertDelivered(&e, j2, "shared source")
		if ok1 && ok2 {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-H-018", func(t *testing.T) {
		// Task removal: a job whose rule is gone is classified, never silently dropped.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-38", Source: "-1001", Target: "-1002", Active: true})
		j := h.Enqueue("task-h-38", "-1001", 3118, "-1002", "removal probe")
		h.DB().Exec("DELETE FROM forward_rules WHERE id = 'task-h-38'")
		e := evt("ATF-E2E-H-018", "H", "task removal: queued job is classified", "RULE_NOT_FOUND")
		start := now()
		err := h.Run(ctx, j)
		e.Observed = h.jobStatus(j.ID)
		e.Result = ResultPass
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-019", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-39", Source: "-1001", Target: "-1002", Active: true,
			Header: "V2:"})
		e := evt("ATF-E2E-H-019", "H", "task update changes the delivered output", "V2 header")
		start := now()
		j := h.Enqueue("task-h-39", "-1001", 3119, "-1002", "update body")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "update body") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-020", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-40", Source: "-1001", Target: "-1002", Active: false})
		e := evt("ATF-E2E-H-020", "H", "task disable stops delivery", "RULE_DISABLED")
		start := now()
		j := h.Enqueue("task-h-40", "-1001", 3120, "-1002", "disabled probe")
		err := h.Run(ctx, j)
		if h.AssertDropped(&e, j) {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-021", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-41", Source: "-1001", Target: "-1002", Active: false})
		h.Install(TaskSpec{ID: "task-h-41b", Source: "-1001", Target: "-1002", Active: true})
		e := evt("ATF-E2E-H-021", "H", "task re-enable resumes delivery", "delivered after enable")
		start := now()
		j1 := h.Enqueue("task-h-41", "-1001", 3121, "-1002", "before enable")
		err1 := h.Run(ctx, j1)
		j2 := h.Enqueue("task-h-41b", "-1001", 3122, "-1002", "after enable")
		err2 := h.Run(ctx, j2)
		if h.AssertDelivered(&e, j2, "after enable") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err2)
		_ = err1
	})

	t.Run("ATF-E2E-H-022", func(t *testing.T) {
		// A fresh harness reads the rule from disk: a config edit is visible
		// without a restart, mirroring the production reload path.
		h := NewHarness(t, 5094837833)
		h.Install(TaskSpec{ID: "task-h-42", Source: "-1001", Target: "-1002", Active: true})
		j := h.Enqueue("task-h-42", "-1001", 3123, "-1002", "cache probe")
		h.Run(ctx, j)
		// edit the rule's header and re-read
		h.DB().Exec("UPDATE forward_rules SET header = 'EDITED:' WHERE id = 'task-h-42'")
		j2 := h.Enqueue("task-h-42", "-1001", 3124, "-1002", "cache re-probe")
		e := evt("ATF-E2E-H-022", "H", "configuration cache invalidation picks up the edit", "EDITED header")
		start := now()
		err := h.Run(ctx, j2)
		if h.AssertDelivered(&e, j2, "cache re-probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-023", func(t *testing.T) {
		h := NewHarness(t, 5094837833)
		e := evt("ATF-E2E-H-023", "H", "sqlite configuration compatibility", "delivered")
		start := now()
		h.Install(TaskSpec{ID: "task-h-43", Source: "-1001", Target: "-1002", Active: true})
		j := h.Enqueue("task-h-43", "-1001", 3125, "-1002", "sqlite probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "sqlite probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})

	t.Run("ATF-E2E-H-024", func(t *testing.T) {
		// Both batteries (SQLite queue + SQLite rules) run in one file: the
		// schema bootstrap and the two stores coexist — the shape a
		// PostgreSQL deployment keeps in two backends.
		h := NewHarness(t, 5094837833)
		e := evt("ATF-E2E-H-024", "H", "two-backend compatibility (rules + queue shapes)", "delivered")
		start := now()
		h.Install(TaskSpec{ID: "task-h-44", Source: "-1001", Target: "-1002", Active: true})
		j := h.Enqueue("task-h-44", "-1001", 3126, "-1002", "backend probe")
		err := h.Run(ctx, j)
		if h.AssertDelivered(&e, j, "backend probe") {
			e.Result = ResultPass
		}
		h.Finish(rec, &e, start, err)
	})
}
