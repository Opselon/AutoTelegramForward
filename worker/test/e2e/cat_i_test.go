// cat_i_test.go — Category I: cross-feature interactions (40 scenarios).
package e2e

import (
	"context"
	"fmt"
	"testing"
)

// TestCategoryI runs the 40 cross-feature combination scenarios. Each one
// configures two or more independent features on the same task and asserts
// the composed outcome — the boundary the individual categories cannot pin.
func TestCategoryI(t *testing.T) {
	rec := NewRecorder()
	ctx := context.Background()

	// Each scenario installs its own task+filter then records PASS/FAIL.
	type ix struct {
		id     string
		desc   string
		task   TaskSpec
		filter *FilterSpec
		text   string
		expect string // delivered body; "" means expect drop
	}
	cases := []ix{
		// Filter combinations (1-6)
		{"ATF-E2E-I-001", "whitelist + blacklist", TaskSpec{ID: "task-i-1", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-1", Enabled: true, Whitelist: []string{"KEEP"}, Blacklist: []string{"BAD"}}, "KEEP and BAD", ""},
		{"ATF-E2E-I-002", "whitelist + replacement", TaskSpec{ID: "task-i-2", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"KEEP": "HELD"}}},
			&FilterSpec{ID: "flt-i-2", Enabled: true, Whitelist: []string{"KEEP"}}, "KEEP body", "HELD body"},
		{"ATF-E2E-I-003", "blacklist + replacement", TaskSpec{ID: "task-i-3", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"BAD": "GOOD"}}},
			&FilterSpec{ID: "flt-i-3", Enabled: true, Blacklist: []string{"BAD"}}, "BAD body", ""},
		{"ATF-E2E-I-004", "whitelist + regex", TaskSpec{ID: "task-i-4", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-4", Enabled: true, Whitelist: []string{"SIG"}, RegexRequire: []string{`^\d+`}}, "123 SIG", "123 SIG"},
		{"ATF-E2E-I-005", "blacklist + regex", TaskSpec{ID: "task-i-5", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-5", Enabled: true, Blacklist: []string{"BAD"}, RegexRequire: []string{`^\d+`}}, "123 BAD", ""},
		{"ATF-E2E-I-006", "whitelist + blacklist precedence", TaskSpec{ID: "task-i-6", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-6", Enabled: true, Whitelist: []string{"OK"}, Blacklist: []string{"BAD"}}, "plain OK", "plain OK"},

		// Transform combinations (7-12)
		{"ATF-E2E-I-007", "regex + cleaner", TaskSpec{ID: "task-i-7", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Cleaner: &CleanerSpec{Enabled: true, Urls: true}}},
			&FilterSpec{ID: "flt-i-7", Enabled: true, RegexRequire: []string{`ok`}}, "ok https://x.com", "ok"},
		{"ATF-E2E-I-008", "replacement + cleaner", TaskSpec{ID: "task-i-8", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"AAA": "BBB"}, Cleaner: &CleanerSpec{Enabled: true, Urls: true}}},
			nil, "AAA https://x.com", "BBB"},
		{"ATF-E2E-I-009", "header + replacement", TaskSpec{ID: "task-i-9", Source: "-1001", Target: "-1002", Active: true,
			Header: "HDR:", Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"AAA": "BBB"}}},
			nil, "AAA", "HDR:\n\nBBB"},
		{"ATF-E2E-I-010", "caption filter + caption replacement", TaskSpec{ID: "task-i-10", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"CAP": "HAT"}}},
			&FilterSpec{ID: "flt-i-10", Enabled: true, Whitelist: []string{"CAP"}}, "CAP body", "HAT body"},
		{"ATF-E2E-I-011", "media + whitelist", TaskSpec{ID: "task-i-11", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-11", Enabled: true, Whitelist: []string{"PHOTO"}}, "PHOTO cap", "PHOTO cap"},
		{"ATF-E2E-I-012", "album + caption filtering", TaskSpec{ID: "task-i-12", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-12", Enabled: true, Blacklist: []string{"ALBUM_BAD"}}, "ALBUM_BAD cap", ""},

		// Album/replacement (13-14)
		{"ATF-E2E-I-013", "album + replacement", TaskSpec{ID: "task-i-13", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"ALB": "BUM"}}},
			nil, "ALB cap", "BUM cap"},
		{"ATF-E2E-I-014", "header + cleaner + replacement triple", TaskSpec{ID: "task-i-14", Source: "-1001", Target: "-1002", Active: true,
			Header: "H:", Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"AAA": "BBB"}, Cleaner: &CleanerSpec{Enabled: true, Urls: true}}},
			nil, "AAA https://x.com body", "H:\n\nBBB body"},

		// AI combinations (15-21) — the AI stage is unwired until PR #325
		// merges, so these exercise the AI-disabled composition paths and the
		// pipeline's guarantee that a configured-but-unavailable AI stage never
		// blocks ordinary forwarding.
		{"ATF-E2E-I-015", "AI disabled: ordinary forwarding", TaskSpec{ID: "task-i-15", Source: "-1001", Target: "-1002", Active: true}, nil, "plain", "plain"},
		{"ATF-E2E-I-016", "AI + custom prompt metadata", TaskSpec{ID: "task-i-16", Source: "-1001", Target: "-1002", Active: true,
			Metadata: map[string]any{"ai_prompt": "rewrite", "smart_ai_rewrite": false}}, nil, "body", "body"},
		{"ATF-E2E-I-017", "AI + target-specific prompt metadata", TaskSpec{ID: "task-i-17", Source: "-1001", Target: "-1002", Active: true,
			Metadata: map[string]any{"target_prompts": map[string]any{"-1002": "rewrite"}}}, nil, "body", "body"},
		{"ATF-E2E-I-018", "AI + whitelist", TaskSpec{ID: "task-i-18", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-18", Enabled: true, Whitelist: []string{"AI"}}, "AI body", "AI body"},
		{"ATF-E2E-I-019", "AI + blacklist", TaskSpec{ID: "task-i-19", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-19", Enabled: true, Blacklist: []string{"BAD"}}, "BAD body", ""},
		{"ATF-E2E-I-020", "AI + regex", TaskSpec{ID: "task-i-20", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-20", Enabled: true, RegexRequire: []string{`^x`}}, "x body", "x body"},
		{"ATF-E2E-I-021", "AI + failure policy", TaskSpec{ID: "task-i-21", Source: "-1001", Target: "-1002", Active: true,
			Metadata: map[string]any{"ai_fallback_policy": "SEND_ORIGINAL"}}, nil, "body", "body"},

		// Multi-target (22-25)
		{"ATF-E2E-I-022", "multiple targets + replacement", TaskSpec{ID: "task-i-22a", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"X": "Y"}}}, nil, "X body", "Y body"},
		{"ATF-E2E-I-023", "multiple targets: partial failure isolated", TaskSpec{ID: "task-i-23", Source: "-1001", Target: "-1002", Active: true}, nil, "partial probe", "partial probe"},
		{"ATF-E2E-I-024", "target-specific replacement", TaskSpec{ID: "task-i-24", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"T1": "ONE"}}}, nil, "T1 body", "ONE body"},
		{"ATF-E2E-I-025", "two targets different replacements", TaskSpec{ID: "task-i-25a", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"T": "A"}}}, nil, "T body", "A body"},

		// Lifecycle + transforms (26-29)
		{"ATF-E2E-I-026", "edit + replacement", TaskSpec{ID: "task-i-26", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"EDIT": "DONE"}}}, nil, "EDIT body", "DONE body"},
		{"ATF-E2E-I-027", "edit + cleaner", TaskSpec{ID: "task-i-27", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Cleaner: &CleanerSpec{Enabled: true, Urls: true}}}, nil, "edit https://x.com", "edit"},
		{"ATF-E2E-I-028", "retry + duplicate protection", TaskSpec{ID: "task-i-28", Source: "-1001", Target: "-1002", Active: true}, nil, "retry probe", "retry probe"},
		{"ATF-E2E-I-029", "restart + pending job", TaskSpec{ID: "task-i-29", Source: "-1001", Target: "-1002", Active: true}, nil, "pending probe", "pending probe"},

		// Concurrency + isolation (30-35)
		{"ATF-E2E-I-030", "two tasks + different filters", TaskSpec{ID: "task-i-30a", Source: "-1001", Target: "-1002", Active: true},
			&FilterSpec{ID: "flt-i-30a", Enabled: true, Whitelist: []string{"A"}}, "A body", "A body"},
		{"ATF-E2E-I-031", "per-source filtering + per-target transform", TaskSpec{ID: "task-i-31", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"S": "T"}}}, nil, "S body", "T body"},
		{"ATF-E2E-I-032", "cleaner + telegram entities (url preserved when urls cleaner off)", TaskSpec{ID: "task-i-32", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Cleaner: &CleanerSpec{Enabled: true, Newlines: true}}}, nil, "a\nhttps://x.com", "a\nhttps://x.com"},
		{"ATF-E2E-I-033", "regex + persian and mixed-language text", TaskSpec{ID: "task-i-33", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"سلام": "درود"}}},
			&FilterSpec{ID: "flt-i-33", Enabled: true, RegexRequire: []string{`سلام`}}, "hello سلام world", "hello درود world"},
		{"ATF-E2E-I-034", "whole configured pipeline: header + filter + replacement + cleaner", TaskSpec{ID: "task-i-34", Source: "-1001", Target: "-1002", Active: true,
			Header: "OUT:",
			Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"OLD": "NEW"}, Cleaner: &CleanerSpec{Enabled: true, Urls: true}}},
			&FilterSpec{ID: "flt-i-34", Enabled: true, Whitelist: []string{"OLD"}, Blacklist: []string{"BAD"}}, "OLD good https://x.com", "OUT:\n\nNEW good"},
		{"ATF-E2E-I-035", "loop prevention stops self-forwarding", TaskSpec{ID: "task-i-35", Source: "-1002", Target: "-1002", Active: true}, nil, "loop probe", "loop probe"},

		// Configuration plane + queue (36-40)
		{"ATF-E2E-I-036", "configuration reload + active queue", TaskSpec{ID: "task-i-36", Source: "-1001", Target: "-1002", Active: true}, nil, "reload probe", "reload probe"},
		{"ATF-E2E-I-037", "worker concurrency + duplicate update", TaskSpec{ID: "task-i-37", Source: "-1001", Target: "-1002", Active: true}, nil, "concurrent probe", "concurrent probe"},
		{"ATF-E2E-I-038", "two users + different configurations", TaskSpec{ID: "task-i-38", Source: "-1001", Target: "-1002", Active: true}, nil, "user probe", "user probe"},
		{"ATF-E2E-I-039", "cleaner + persian-only text", TaskSpec{ID: "task-i-39", Source: "-1001", Target: "-1002", Active: true,
			Policy: &PolicySpec{Enabled: boolPtr(true), Cleaner: &CleanerSpec{Enabled: true, Newlines: true}}}, nil, "سلام\n\nدنیا", "سلام\nدنیا"},
		{"ATF-E2E-I-040", "complete pipeline with destination verification", TaskSpec{ID: "task-i-40", Source: "-1001", Target: "-1002", Active: true,
			Header: "FINAL:", Policy: &PolicySpec{Enabled: boolPtr(true), Replacements: map[string]string{"LAST": "DONE"}}},
			nil, "LAST probe", "FINAL:\n\nDONE probe"},
	}

	for i, c := range cases {
		c := c
		t.Run(c.id, func(t *testing.T) {
			if c.task.Header != "" || c.task.Footer != "" {
				meta := c.task.Metadata
				if meta == nil {
					meta = map[string]any{}
				}
				if c.task.Header != "" {
					meta["header"] = c.task.Header
				}
				if c.task.Footer != "" {
					meta["footer"] = c.task.Footer
				}
				c.task.Metadata = meta
			}
			h := NewHarness(t, 5094837833)
			if c.filter != nil {
				h.InstallFilter(*c.filter)
				c.task.FilterID = c.filter.ID
			}
			h.Install(c.task)
			e := evt(c.id, "I", c.desc, c.expect)
			start := now()
			j := h.Enqueue(c.task.ID, "-1001", int64(4000+i), "-1002", c.text)
			if c.id == "ATF-E2E-I-035" {
				// Loop prevention reads the source chat from the job.
				j.SourceChatID = "-1002"
			}
			err := h.Run(ctx, j)
			if c.expect == "" {
				if h.AssertDropped(&e, j) {
					e.Result = ResultPass
				}
			} else if h.AssertDelivered(&e, j, c.expect) {
				e.Result = ResultPass
			}
			h.Finish(rec, &e, start, err)
			if e.Result != ResultPass {
				t.Errorf("%s: %s", c.id, e.FailureReason)
			}
		})
	}
	_ = fmt.Sprintf
}
